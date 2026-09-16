# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The relay protocol, and the only module here that builds a URL.

Mirrors `accounts/gvaccounts/relay.py` in the GammonView repo route for route.
Where a shape looks arbitrary it is matching that file, and the comments say so
rather than re-deriving the reason -- the relay is the specification and this is
a client of it.

Two clients, not one, because the two halves authenticate differently and that
partition is the security boundary the whole design rests on:

  * `PairingClient` holds a `secret` that is good for one pairing and grants
    nothing else. It has no token because collecting one is what it is for.
  * `WorkerClient` holds the worker session and never touches the pairing
    routes again.

Keeping them separate means there is no object in this process that can both
mint a credential and use one.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import httpx

from . import USER_AGENT
from .config import Config

#: Which revision of the relay protocol this client implements.
#:
#: The server half of this protocol lives in a **different repository** and is
#: not public (`accounts/gvaccounts/relay.py` in GammonView). That split is
#: deliberate -- a public client, a private server -- but it means neither side
#: can be changed with the other in view, and a drift shows up as a helper that
#: pairs fine and then silently never claims a job.
#:
#: So the contract gets a number. It is sent in the `User-Agent` rather than in
#: any request body, for two reasons: the relay's models were written before
#: this existed and an unexpected field is a 422 on some of them, and the
#: access log is exactly where you want this when you are looking at one
#: machine that stopped working. `docs/Relay.md` records what each revision
#: means; `tests/test_contract.py` checks a live relay still answers it.
#:
#: Bump it when a route, a field or a status code changes meaning -- not when
#: this package is released, which is what the version above is for.
RELAY_CONTRACT = "1"

#: What this client sends as its `User-Agent`: the package version and the
#: contract it speaks. The relay's `version` field is deliberately *not* this
#: string -- that one is shown to the user in account settings, where a
#: protocol revision is noise.
CLIENT_AGENT = f"{USER_AGENT} (relay/{RELAY_CONTRACT})"

#: How long a plain request may take. Generous because the far end is a shared
#: VPS answering other people too, and a helper that gives up at three seconds
#: on a slow morning reads to its owner as "the helper is broken".
REQUEST_TIMEOUT = 30.0

#: The long poll's ceiling. The relay holds `next-job` for at most
#: `MAX_WAIT_SECONDS` (30) and nginx reaps an upstream at 60, so the client's
#: patience has to exceed the server's intent and stay under the proxy's --
#: 45 is the middle of that band. Too low and every empty poll looks like a
#: network failure; too high and a genuinely dead connection sits undetected.
POLL_TIMEOUT = 45.0

#: What the helper asks the relay to hold a poll open for. The server clamps to
#: its own maximum, so asking for more is harmless -- but asking for *exactly*
#: the server's maximum means a slow round trip lands after the client gave up,
#: so this is deliberately a little under `POLL_TIMEOUT`.
POLL_WAIT = 30


class RelayError(Exception):
    """A request the relay refused, carrying a message fit to show a user."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Unauthorized(RelayError):
    """The worker token is gone: unlinked in settings, or the account deleted.

    Its own type because it is the one error the daemon must not retry. Every
    other failure is worth another poll in thirty seconds; this one will still
    be a 401 tomorrow, and the only cure is pairing again.
    """


def _message(response: httpx.Response, fallback: str) -> str:
    """The server's own words where there are any.

    The relay writes its refusals for people -- "That is not the word shown on
    the computer you are linking" -- and nginx writes HTML for 413 and 502. So
    the JSON is tried and the fallback is used when it is not JSON, which is the
    same tolerance `accounts.js` shows on the browser side.
    """
    try:
        detail = response.json().get("detail")
        if isinstance(detail, str) and detail:
            return detail
    except Exception:
        pass
    return fallback


def _check(response: httpx.Response, fallback: str) -> httpx.Response:
    if response.status_code == 401:
        raise Unauthorized(_message(response, "This computer is no longer linked."), 401)
    if response.status_code >= 400:
        raise RelayError(_message(response, fallback), response.status_code)
    return response


@dataclass
class Job:
    """One unit of work, as handed over by `next-job`."""

    id: str
    preset: str
    match_bytes: bytes
    lease_seconds: int


class PairingClient:
    """The device flow, from the helper's side.

    It knows two secrets and keeps them apart, which is the point of the whole
    exchange: `code` identifies the pairing and travels in a URL that anybody
    might see, while `secret` never leaves this process and is what actually
    collects the token. Handing one value to both jobs is the bug the relay's
    `test_the_link_alone_cannot_collect_the_token` exists to catch.
    """

    def __init__(self, cfg: Config, client: httpx.Client | None = None):
        self.cfg = cfg
        self._client = client or httpx.Client(
            timeout=REQUEST_TIMEOUT, headers={"User-Agent": CLIENT_AGENT}
        )

    def start(self, machine: str, platform: str) -> dict:
        """Ask to be linked. Returns `code`, `secret`, `word`, `choices`.

        Unauthenticated, and grants nothing -- all it creates is an offer that
        expires in a minute and that no account has agreed to.
        """
        r = self._client.post(
            f"{self.cfg.relay_base}/pair/start",
            json={"machine": machine, "platform": platform},
        )
        _check(r, "Could not start linking. Please try again in a moment.")
        return r.json()

    def poll(self, code: str, secret: str, wait: int = POLL_WAIT) -> dict:
        """Wait for somebody to confirm. `{"status": "pending"}` until they do.

        A 404 here is the expected end of the unhappy path rather than a fault:
        the pairing expired, or somebody picked the wrong word and the relay
        deleted the row. Either way the answer is to start again, so it is left
        to the caller to phrase.
        """
        r = self._client.post(
            f"{self.cfg.relay_base}/pair/poll",
            params={"wait": wait},
            json={"code": code, "secret": secret},
            timeout=POLL_TIMEOUT,
        )
        _check(r, "Linking failed. Please start again.")
        return r.json()


class WorkerClient:
    """The worker half: register, claim, report, deliver.

    One `httpx.Client` for the life of the daemon, so the TLS handshake is paid
    once rather than once per poll -- which over a day of thirty-second polls is
    the difference between a few thousand handshakes and one.
    """

    def __init__(self, cfg: Config, token: str, client: httpx.Client | None = None):
        self.cfg = cfg
        self._client = client or httpx.Client(
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": CLIENT_AGENT, "Authorization": f"Bearer {token}"},
        )

    @property
    def _base(self) -> str:
        return f"{self.cfg.relay_base}/worker"

    def hello(self, name: str, version: str, engine: str, presets: list[str]) -> None:
        """Register this machine and what it can do.

        The preset list is the useful half. The site gates its menu on what a
        live helper reports, so this call is how *World Class* appears in a
        browser that would otherwise only offer the shared worker's three.
        """
        _check(
            self._client.post(
                f"{self._base}/hello",
                json={"name": name, "version": version, "engine": engine, "presets": presets},
            ),
            "Could not register this computer.",
        )

    def next_job(self, wait: int = POLL_WAIT) -> Job | None:
        """Hold a poll open until there is work, or `wait` elapses.

        Returns None on an empty wait. The relay answers `{"job": null}` rather
        than a 204 precisely so this has one shape to parse.
        """
        r = self._client.get(
            f"{self._base}/next-job", params={"wait": wait}, timeout=POLL_TIMEOUT
        )
        _check(r, "Could not ask for work.")
        job = r.json().get("job")
        if not job:
            return None
        return Job(
            id=job["id"],
            preset=job.get("preset") or "",
            # base64 rather than a second round trip: the handout is JSON
            # because it carries the preset too, and a 64 KB cap makes the 4/3
            # expansion cheap. The result goes back as raw bytes instead.
            match_bytes=base64.b64decode(job["match"]),
            lease_seconds=int(job.get("lease_seconds") or 0),
        )

    def progress(self, job_id: str, done: int, total: int) -> None:
        """Report counts, which is also how the claim stays alive.

        The liveness signal and the progress signal are the same signal. The
        helper has these numbers anyway because the browser wants a progress
        bar, so holding the lease costs nothing extra -- and a helper that stops
        advancing stops renewing, which is exactly when the job should go back
        in the queue.
        """
        _check(
            self._client.post(
                f"{self._base}/jobs/{job_id}/progress",
                json={"decisions_done": done, "decisions_total": total},
            ),
            "Could not report progress.",
        )

    def deliver(self, job_id: str, result: bytes) -> None:
        """Hand back the gzipped `.gvab`. A raw body: this way is bytes only."""
        _check(
            self._client.post(
                f"{self._base}/jobs/{job_id}/result",
                content=result,
                headers={"Content-Type": "application/octet-stream"},
                # A 4 MB upload on a domestic connection can outrun the default.
                timeout=120.0,
            ),
            "Could not deliver the analysis.",
        )

    def fail(self, job_id: str, error: str) -> None:
        """Give up on a job, with a reason the browser will show verbatim.

        Verbatim because these messages come from the same `gvanalysis` the
        shared worker runs, so they are the sentences this app's users already
        see. A second vocabulary for the same failures would be two things to
        keep true.
        """
        _check(
            self._client.post(
                f"{self._base}/jobs/{job_id}/error", json={"error": error[:500]}
            ),
            "Could not report the failure.",
        )

    def close(self) -> None:
        self._client.close()

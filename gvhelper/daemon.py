# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The loop: ask for work, do it, hand it back, ask again.

Everything interesting about this file is how it fails, because a helper spends
almost all of its life waiting and the waiting is where the bugs are.

**A network failure is not an error, it is weather.** A laptop sleeps, changes
network, loses wifi in a lift. Every one of those surfaces as an exception from
a poll, and the right response to all of them is to wait a moment and poll
again. So the loop retries with a backoff and says so quietly, rather than
exiting and leaving a user to discover hours later that their helper stopped.

**The one exception is a 401**, which is the relay saying this machine is not
linked any more -- unlinked from account settings, or the account deleted. That
will still be true tomorrow, so it stops the loop and says what to do.

**A failed analysis is reported, not swallowed.** If `gvanalysis` raises, the
job is failed with the engine's own message and the loop continues. The
alternative -- dropping it -- leaves the browser on a spinner until the relay's
lease expires, which is minutes of a person watching nothing happen.
"""

from __future__ import annotations

import sys
import threading
import time

from . import USER_AGENT, __version__
from .client import Job, RelayError, Unauthorized, WorkerClient
from .config import Config, machine_name
from .runner import Progress, analyze, available_presets, engine_version, lower_priority

#: How often progress is posted while a job runs.
#:
#: **One second, because that is what the browser polls at.** This number's
#: only consumer is a progress bar somebody is watching, and `gva/analyze.js`
#: asks the relay for it every `POLL_INTERVAL_MS` = 1000ms -- so anything
#: slower than that is a bar moving in visible steps while the browser asks
#: five times per step for news that has not changed. It was 5s, chosen
#: against the two bounds below without checking the rate at the other end.
#:
#: Those bounds are both still satisfied with room to spare, which is how the
#: wrong number survived: it must be far under the relay's 300s lease or a
#: long decision looks like a hang, and far over one decision's cost (tens of
#: milliseconds) or the helper spends its time reporting instead of analysing.
#: 1s sits 300x under one and ~30x over the other.
#:
#: The cost is one small POST a second for the length of a run, which also
#: renews the lease -- the shared worker gets this for free by writing counts
#: to a `Manager` dict on every decision (`gvserver/jobs.py`), an option a
#: process on somebody else's computer does not have.
PROGRESS_INTERVAL = 1.0

#: Backoff after a failed poll: start here, double, stop at the ceiling.
#: The ceiling is a minute because that is roughly how long a person waits
#: before wondering whether the thing is working, and the floor is two seconds
#: so a blip costs nothing noticeable.
RETRY_MIN = 2.0
RETRY_MAX = 60.0


def _log(message: str) -> None:
    """One line, flushed, with a clock on it.

    stdout rather than a logging framework: the helper runs under launchd or
    systemd, both of which capture stdout into the platform's own log, and
    a user asked to send their log should be able to read it first.
    """
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def run_job(client: WorkerClient, job: Job, cfg: Config) -> None:
    """Analyse one job and deliver it, or report why not.

    The analysis runs on a thread so this one can keep posting progress -- which
    is also what renews the lease, so a job that takes ten minutes is not
    reclaimed at five. `daemon=True` on the thread is deliberate: if the helper
    is killed mid-analysis the process should go, not hang waiting for an
    engine to finish work nobody is waiting for any more.
    """
    progress = Progress()
    result: dict = {}

    def work() -> None:
        try:
            result["bytes"] = analyze(
                job.match_bytes, job.preset, progress, jobs=cfg.jobs, threads=cfg.threads
            )
        except BaseException as e:  # noqa: BLE001 - reported, then re-raised to nobody
            result["error"] = e

    thread = threading.Thread(target=work, name=f"analyze-{job.id}", daemon=True)
    _log(f"analyzing {job.id} ({job.preset or 'default preset'})")
    thread.start()

    last_posted: tuple[int, int] | None = None
    while thread.is_alive():
        thread.join(PROGRESS_INTERVAL)
        done, total = progress.get()
        # Posted even when the counts have not moved, because this doubles as
        # the lease renewal: a single very slow decision must not read as a
        # stalled worker. Skipped only before the total is known, when there is
        # nothing to say and the relay would record a total of zero.
        if total or last_posted:
            try:
                client.progress(job.id, done, total)
                last_posted = (done, total)
            except Unauthorized:
                raise
            except RelayError as e:
                # Not fatal. The analysis is the expensive part and it is still
                # running; a progress post that failed will be retried five
                # seconds from now, and the lease has minutes on it.
                _log(f"progress not posted: {e}")

    if "error" in result:
        error = result["error"]
        _log(f"job {job.id} failed: {error}")
        client.fail(job.id, f"{type(error).__name__}: {error}")
        return

    payload = result.get("bytes")
    if not payload:  # pragma: no cover - a thread that neither raised nor produced
        client.fail(job.id, "The analysis produced nothing.")
        return

    client.deliver(job.id, payload)
    done, total = progress.get()
    _log(f"delivered {job.id} ({total or done} decisions, {len(payload)} bytes)")


def serve(cfg: Config, token: str, *, once: bool = False) -> int:
    """Poll for work until something stops us. Returns a process exit code.

    `once` runs a single poll-and-work cycle, which is what the tests use and
    what makes a "does this actually work" check possible without a signal.
    """
    lower_priority(cfg.nice)
    client = WorkerClient(cfg, token)
    name = machine_name()

    try:
        client.hello(name, __version__, engine_version(), available_presets())
    except Unauthorized as e:
        _log(str(e))
        return 2
    except RelayError as e:
        # Not fatal: `hello` is how the site learns this machine's presets, and
        # a helper that cannot register can still claim work. Failing to start
        # over a cosmetic call would be the wrong trade.
        _log(f"could not register: {e}")

    _log(f"{USER_AGENT} linked as {name!r} -> {cfg.api_base}")
    _log(f"presets: {', '.join(available_presets()) or '(engine not installed)'}")

    delay = RETRY_MIN
    try:
        while True:
            try:
                job = client.next_job()
                delay = RETRY_MIN
                if job is not None:
                    run_job(client, job, cfg)
                if once:
                    return 0
            except Unauthorized as e:
                _log(str(e))
                _log("Run `gammonview-helper link` to link this computer again.")
                return 2
            except RelayError as e:
                # The status is part of the message because of what the first
                # real failure looked like: "Could not ask for work." repeated
                # every few minutes, with nothing to say whether that was the
                # relay refusing, the account gone, or -- as it turned out -- a
                # proxy 504. A number here would have named it immediately.
                where = f" (HTTP {e.status})" if e.status else ""
                _log(f"{e}{where} -- retrying in {delay:.0f}s")
            except Exception as e:  # noqa: BLE001 - weather, per the module docstring
                _log(f"{type(e).__name__}: {e} -- retrying in {delay:.0f}s")
            else:
                continue
            if once:
                return 1
            time.sleep(delay)
            delay = min(RETRY_MAX, delay * 2)
    except KeyboardInterrupt:
        _log("stopped")
        return 0
    finally:
        client.close()


def main() -> int:  # pragma: no cover - thin wrapper, exercised via the CLI
    from .cli import main as cli_main

    return cli_main(["run"] + sys.argv[1:])

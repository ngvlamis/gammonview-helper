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
from .store import load_token

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


class Control:
    """What the menu-bar item can ask of the loop, and what it reads back.

    The loop runs on a background thread under the menu (AppKit owns the main
    one), so this is the whole of the interface between them: two requests
    going in -- pause, stop -- and a few facts coming out for the menu to draw.
    The terminal path builds one too and never touches it, so there is a
    single loop rather than a menu-bar variant of it.

    **Pause lets the current match finish.** It was queued from somebody's own
    browser and is partly done; abandoning it would cost them the work and a
    five-minute wait for the lease to lapse. Pause stops the *next* claim.

    **Stop does not.** Quit is the user saying the machine is wanted for
    something else right now, so the running job is failed with a sentence the
    browser shows, rather than left to time out.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._paused = False
        self._stopping = False
        #: "starting" | "unlinked" | "idle" | "busy" | "paused" | "retrying"
        self.state = "starting"
        #: The running job's counters, or None between jobs.
        self.progress: Progress | None = None
        #: The email of the account this machine is linked to, once `hello`
        #: has said; None before that, when unlinked, and on an older site.
        self.account: str | None = None

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def stopping(self) -> bool:
        return self._stopping

    def pause(self) -> None:
        with self._cond:
            self._paused = True
            self._cond.notify_all()

    def resume(self) -> None:
        with self._cond:
            self._paused = False
            self._cond.notify_all()

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()

    def wait_while_paused(self) -> None:
        """Block until resumed or stopped."""
        with self._cond:
            self._cond.wait_for(lambda: not self._paused or self._stopping)

    def sleep(self, seconds: float) -> None:
        """`time.sleep`, cut short by `stop` -- so Quit never waits out a backoff."""
        with self._cond:
            self._cond.wait_for(lambda: self._stopping, timeout=seconds)


def _sleep(control: Control | None, seconds: float) -> None:
    # `time.sleep` when nobody is listening, which is what the tests replace.
    if control is None:
        time.sleep(seconds)
    else:
        control.sleep(seconds)


#: What the browser shows when the helper is quit mid-analysis.
QUIT_MESSAGE = "GammonView Helper was quit on {machine} before this analysis finished."


def sign_off(client: WorkerClient) -> None:
    """Tell the site to stop routing work here. Best effort, and never raises.

    Failing to send it costs what it cost before the route existed -- two
    minutes of the site believing a quiet helper is still there -- so it is not
    worth surfacing, let alone worth refusing to pause over.
    """
    try:
        client.offline()
    except Exception as e:  # noqa: BLE001 - best effort, per the docstring
        _log(f"could not tell the site this computer is pausing: {e}")


def _log(message: str) -> None:
    """One line, flushed, with a clock on it.

    stdout rather than a logging framework: the helper runs under launchd or
    systemd, both of which capture stdout into the platform's own log, and
    a user asked to send their log should be able to read it first.
    """
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def run_job(client: WorkerClient, job: Job, cfg: Config, control: Control | None = None) -> None:
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
    if control is not None:
        control.progress = progress
    thread.start()

    last_posted: tuple[int, int] | None = None
    while thread.is_alive():
        thread.join(PROGRESS_INTERVAL)
        if control is not None and control.stopping:
            # The engine thread is a daemon and goes with the process; what
            # must not go with it is the browser's answer.
            _log(f"job {job.id} abandoned: the helper is quitting")
            client.fail(job.id, QUIT_MESSAGE.format(machine=machine_name()))
            return
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


#: How often an unlinked helper looks in the credential store.
#:
#: The number exists because pairing and polling are two processes. `link`
#: registers the worker with the relay *immediately*, so the website starts
#: offering this machine work at once -- but the daemon only finds the new
#: credential when it next looks. Anything it waits here is time the site
#: believes there is a helper and no helper is listening.
#:
#: Measured, not guessed: the first job on gammonview.com sat queued for 31
#: seconds, which was the login item's restart throttle, not the engine. Three
#: seconds puts that gap under the one-second dispatch latency plus a blink,
#: and costs a keyring read every three seconds on a machine that is by
#: definition doing nothing else.
LINK_POLL_INTERVAL = 3.0

#: How often waiting says so out loud. The wait is unbounded -- a login item
#: on a machine nobody has paired yet is a correct state that can last for
#: days -- so this is the difference between a log and a log file.
LINK_LOG_INTERVAL = 900.0


def wait_for_token(
    cfg: Config, *, current: str | None = None, control: Control | None = None
) -> str | None:
    """Block until the credential store holds a usable token.

    Returns None only when `control` is stopped -- Quit from the menu while the
    helper is waiting to be linked.

    Two callers, one behaviour. A helper that has never been linked waits with
    `current=None`; a helper whose token the relay just rejected waits with the
    dead one, so that re-reading the same revoked credential does not count as
    success and spin.

    **This is why the daemon no longer exits when it is not linked.** It used
    to return 2, which under a login item means: launchd restarts it, it exits
    again, and the restart throttle -- 60 seconds, chosen to keep exactly that
    loop quiet -- becomes the delay before a freshly paired machine starts
    working. Waiting in-process instead removes the loop, so there is nothing
    to throttle, and it does so identically on macOS, Linux and Windows rather
    than needing `launchctl kickstart`, `systemctl --user restart`, and
    whatever Windows would have wanted.
    """
    said_at = 0.0
    if control is not None:
        control.state = "unlinked"
        control.account = None
    while True:
        if control is not None and control.stopping:
            return None
        token = load_token(cfg)
        if token and token != current:
            return token
        now = time.monotonic()
        if said_at == 0.0 or now - said_at >= LINK_LOG_INTERVAL:
            _log("not linked yet -- waiting for `gammonview-helper link`")
            said_at = now
        _sleep(control, LINK_POLL_INTERVAL)


def serve(
    cfg: Config, token: str, *, once: bool = False, control: Control | None = None
) -> int:
    """Poll for work until something stops us. Returns a process exit code.

    `once` runs a single poll-and-work cycle, which is what the tests use and
    what makes a "does this actually work" check possible without a signal.

    `control` is the menu-bar item's handle on the loop; see `Control`. Pause
    and stop are both checked between polls, so a held poll (up to 25s) runs
    out first -- and anything it claims in that time is run, because a claimed
    job has nowhere else to go until its lease lapses.
    """
    lower_priority(cfg.nice)
    client = WorkerClient(cfg, token)
    name = machine_name()

    try:
        account = client.hello(name, __version__, engine_version(), available_presets())
        if control is not None:
            control.account = account
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
    signed_off = False
    try:
        while True:
            if control is not None:
                if control.stopping:
                    return 0
                if control.paused:
                    # Said once per pause rather than once per loop: the
                    # menu has normally said it already, and this is the
                    # retry for when that attempt could not reach the site.
                    if not signed_off:
                        sign_off(client)
                        signed_off = True
                        _log("paused")
                    control.state = "paused"
                    control.wait_while_paused()
                    continue
                if signed_off:
                    _log("resumed")
                    signed_off = False
                control.state = "idle"
            try:
                job = client.next_job()
                delay = RETRY_MIN
                if job is not None:
                    if control is not None:
                        control.state = "busy"
                    try:
                        run_job(client, job, cfg, control)
                    finally:
                        if control is not None:
                            control.progress = None
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
            if control is not None:
                control.state = "retrying"
            _sleep(control, delay)
            delay = min(RETRY_MAX, delay * 2)
    except KeyboardInterrupt:
        _log("stopped")
        return 0
    finally:
        client.close()


def main() -> int:  # pragma: no cover - thin wrapper, exercised via the CLI
    from .cli import main as cli_main

    return cli_main(["run"] + sys.argv[1:])

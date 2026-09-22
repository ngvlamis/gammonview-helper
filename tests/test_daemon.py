# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The loop, and mostly how it fails.

A helper spends almost all of its life waiting, so the waiting is where the
bugs are. These stub the analysis itself -- running a real engine here would
turn a 0.2s suite into a minutes-long one and would test `gvanalysis`, which has
its own goldens -- and concentrate on what the loop does around it.
"""

from __future__ import annotations

import base64
import threading

import httpx
import pytest
import respx

from gvhelper import daemon
from gvhelper.client import RelayError, Unauthorized, WorkerClient

RELAY = "https://example.test/accounts/relay"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """No real sleeping, and no real engine.

    `PROGRESS_INTERVAL` is dropped to a hair so a test can observe the progress
    loop running rather than waiting five seconds for it.
    """
    monkeypatch.setattr(daemon.time, "sleep", lambda s: None)
    monkeypatch.setattr(daemon, "PROGRESS_INTERVAL", 0.01)
    monkeypatch.setattr(daemon, "lower_priority", lambda n: None)
    monkeypatch.setattr(daemon, "engine_version", lambda: "bgsage test")
    monkeypatch.setattr(daemon, "available_presets", lambda: ["fast", "world_class"])


def _job(preset="world_class", match=b"OGXM"):
    return {"job": {"id": "j1", "preset": preset,
                    "match": base64.b64encode(match).decode(), "lease_seconds": 300}}


@respx.mock
def test_a_job_is_analysed_and_delivered(cfg, monkeypatch):
    respx.post(f"{RELAY}/worker/hello").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{RELAY}/worker/next-job").mock(return_value=httpx.Response(200, json=_job()))
    respx.post(f"{RELAY}/worker/jobs/j1/progress").mock(
        return_value=httpx.Response(200, json={})
    )
    result = respx.post(f"{RELAY}/worker/jobs/j1/result").mock(
        return_value=httpx.Response(200, json={})
    )

    seen = {}

    def fake_analyze(match_bytes, preset, progress, **kw):
        seen["preset"] = preset
        seen["bytes"] = match_bytes
        seen["jobs"] = kw.get("jobs")
        progress.set(10, 10)
        return b"gz-result"

    monkeypatch.setattr(daemon, "analyze", fake_analyze)
    assert daemon.serve(cfg, "tok", once=True) == 0
    assert seen["preset"] == "world_class"
    assert seen["bytes"] == b"OGXM"
    assert result.calls.last.request.read() == b"gz-result"


@respx.mock
def test_progress_is_posted_while_the_job_runs(cfg, monkeypatch):
    """This is the lease renewal as much as the progress bar -- the relay
    expires a claim that stops advancing, so a long analysis that posted nothing
    would be reclaimed mid-run and done twice."""
    respx.post(f"{RELAY}/worker/hello").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{RELAY}/worker/next-job").mock(return_value=httpx.Response(200, json=_job()))
    progress = respx.post(f"{RELAY}/worker/jobs/j1/progress").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.post(f"{RELAY}/worker/jobs/j1/result").mock(return_value=httpx.Response(200, json={}))

    release = threading.Event()

    def slow_analyze(match_bytes, preset, p, **kw):
        p.set(1, 100)
        release.wait(2.0)
        return b"gz"

    monkeypatch.setattr(daemon, "analyze", slow_analyze)

    def stop_soon():
        # Let the progress loop turn over a few times, then let the job finish.
        threading.Timer(0.15, release.set).start()

    stop_soon()
    assert daemon.serve(cfg, "tok", once=True) == 0
    assert progress.call_count >= 2


@respx.mock
def test_a_failed_analysis_is_reported_rather_than_dropped(cfg, monkeypatch):
    """Dropping it would leave the browser on a spinner until the relay's lease
    expires -- minutes of a person watching nothing happen."""
    respx.post(f"{RELAY}/worker/hello").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{RELAY}/worker/next-job").mock(return_value=httpx.Response(200, json=_job()))
    respx.post(f"{RELAY}/worker/jobs/j1/progress").mock(return_value=httpx.Response(200, json={}))
    failed = respx.post(f"{RELAY}/worker/jobs/j1/error").mock(
        return_value=httpx.Response(200, json={})
    )

    def boom(*a, **kw):
        raise ValueError("unknown preset 'world_class'")

    monkeypatch.setattr(daemon, "analyze", boom)
    assert daemon.serve(cfg, "tok", once=True) == 0
    assert b"unknown preset" in failed.calls.last.request.read()


@respx.mock
def test_being_unlinked_stops_the_loop(cfg, monkeypatch):
    """The one failure that will still be a failure tomorrow. Retrying it would
    poll a 401 forever and never tell the user the one thing they need to know."""
    respx.post(f"{RELAY}/worker/hello").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{RELAY}/worker/next-job").mock(return_value=httpx.Response(401))
    assert daemon.serve(cfg, "tok", once=True) == 2


@respx.mock
def test_a_network_failure_is_weather_not_an_error(cfg, monkeypatch):
    """A laptop sleeps, changes network, loses wifi in a lift. Exiting on any of
    those leaves a user to discover hours later that their helper stopped."""
    respx.post(f"{RELAY}/worker/hello").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{RELAY}/worker/next-job").mock(side_effect=httpx.ConnectError("down"))
    # `once` turns the retry into a return so the test does not loop forever;
    # what it asserts is that the code is 1 (keep going) and not 2 (stop).
    assert daemon.serve(cfg, "tok", once=True) == 1


@respx.mock
def test_a_helper_that_cannot_register_still_takes_work(cfg, monkeypatch):
    """`hello` is how the site learns this machine's presets -- useful, but not
    worth refusing to start over. A helper that cannot register can still claim
    a job that was already queued."""
    respx.post(f"{RELAY}/worker/hello").mock(return_value=httpx.Response(500))
    respx.get(f"{RELAY}/worker/next-job").mock(return_value=httpx.Response(200, json={"job": None}))
    assert daemon.serve(cfg, "tok", once=True) == 0


@respx.mock
def test_being_unlinked_before_the_first_poll_stops_the_loop(cfg):
    """A 401 on `hello` means the same thing a 401 on a poll does, and the
    tolerance above must not swallow it."""
    respx.post(f"{RELAY}/worker/hello").mock(return_value=httpx.Response(401))
    assert daemon.serve(cfg, "tok", once=True) == 2


@respx.mock
def test_a_progress_post_that_fails_does_not_lose_the_analysis(cfg, monkeypatch):
    """The analysis is the expensive part and it is still running. Abandoning a
    finished run because a status update 500'd would be the worst trade in the
    program."""
    respx.post(f"{RELAY}/worker/hello").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{RELAY}/worker/next-job").mock(return_value=httpx.Response(200, json=_job()))
    respx.post(f"{RELAY}/worker/jobs/j1/progress").mock(return_value=httpx.Response(500))
    result = respx.post(f"{RELAY}/worker/jobs/j1/result").mock(
        return_value=httpx.Response(200, json={})
    )

    def analyze(match_bytes, preset, p, **kw):
        p.set(5, 5)
        return b"gz"

    monkeypatch.setattr(daemon, "analyze", analyze)
    assert daemon.serve(cfg, "tok", once=True) == 0
    assert result.call_count == 1


@respx.mock
def test_the_machines_presets_are_announced_at_startup(cfg):
    """What makes the deeper entries appear in the website's menu."""
    hello = respx.post(f"{RELAY}/worker/hello").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{RELAY}/worker/next-job").mock(return_value=httpx.Response(200, json={"job": None}))
    daemon.serve(cfg, "tok", once=True)
    assert b"world_class" in hello.calls.last.request.read()


def test_the_progress_pair_is_read_atomically():
    """Read as a pair and posted as a pair. A torn read would put a `done` from
    one decision beside a `total` from before the total was known, and the
    browser would draw a bar that jumps backwards."""
    p = daemon.Progress()
    p.set(3, 9)
    assert p.get() == (3, 9)


# --- waiting to be linked ---

def test_an_unlinked_helper_waits_instead_of_exiting(cfg, monkeypatch):
    """It used to return 2. Under a login item that means the supervisor
    restarts it, it exits again, and the restart throttle -- 60s, chosen to
    keep exactly that loop quiet -- becomes the delay before a freshly paired
    machine does any work. Measured on gammonview.com the day accounts went
    live: 31 seconds of a 36-second first analysis, none of it the engine."""
    tokens = iter([None, None, "tok"])
    monkeypatch.setattr(daemon, "load_token", lambda cfg: next(tokens))
    assert daemon.wait_for_token(cfg) == "tok"


def test_a_revoked_token_is_not_mistaken_for_a_new_one(cfg, monkeypatch):
    """The other caller: the relay rejected what we hold. Re-reading the same
    dead credential out of the keyring must not count as an answer, or the
    helper spins on it at the poll interval."""
    tokens = iter(["dead", "dead", "fresh"])
    monkeypatch.setattr(daemon, "load_token", lambda cfg: next(tokens))
    assert daemon.wait_for_token(cfg, current="dead") == "fresh"


def test_waiting_does_not_write_a_line_per_poll(cfg, monkeypatch, capsys):
    """A login item on a machine nobody has paired is a correct state that can
    last for days, so the wait is unbounded and the logging must not be."""
    tokens = iter([None] * 20 + ["tok"])
    monkeypatch.setattr(daemon, "load_token", lambda cfg: next(tokens))
    daemon.wait_for_token(cfg)
    assert capsys.readouterr().out.count("not linked yet") == 1


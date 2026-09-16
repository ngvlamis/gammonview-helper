# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The relay protocol from the helper's side.

These are contract tests against `accounts/gvaccounts/relay.py` in the
GammonView repo. Where one asserts a field name or a shape, the thing it is
really asserting is that this client and that service still agree -- so a test
here failing after a relay change is the change being noticed, which is the
point of writing them this way rather than against a mock of our own design.
"""

from __future__ import annotations

import base64
import time

import httpx
import pytest
import respx

from gvhelper import client as client_module

from gvhelper.client import (
    POLL_GATEWAY_FLOOR,
    POLL_WAIT,
    PairingClient,
    RelayError,
    Unauthorized,
    WorkerClient,
)

RELAY = "https://example.test/accounts/relay"


@respx.mock
def test_starting_a_pairing_sends_what_the_screen_will_show(cfg):
    """The machine name and platform are an unverified claim, and the
    confirmation screen presents them as one. This asserts they are sent at
    all -- without them the screen has nothing to help a person recognise their
    own computer."""
    route = respx.post(f"{RELAY}/pair/start").mock(
        return_value=httpx.Response(
            200,
            json={"code": "c", "secret": "s", "word": "HERON",
                  "choices": ["HERON", "ANVIL", "MAPLE"], "expires_in": 60},
        )
    )
    offer = PairingClient(cfg).start("MacBook Pro", "macOS 15.6")
    assert offer["word"] == "HERON"
    assert route.calls.last.request.read() == b'{"machine":"MacBook Pro","platform":"macOS 15.6"}'


@respx.mock
def test_the_poll_sends_the_secret_and_not_only_the_code(cfg):
    """The two secrets are the whole security argument of the device flow: the
    code rides in a URL and is therefore public, the secret is what actually
    collects the token. A client that polled with the code alone would hand the
    token to anyone holding the link."""
    route = respx.post(f"{RELAY}/pair/poll").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    PairingClient(cfg).poll("the-code", "the-secret")
    body = route.calls.last.request.read()
    assert b"the-secret" in body
    assert b"the-code" in body


@respx.mock
def test_a_pending_pairing_is_not_an_error(cfg):
    respx.post(f"{RELAY}/pair/poll").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    assert PairingClient(cfg).poll("c", "s")["status"] == "pending"


@respx.mock
def test_the_relays_own_words_reach_the_user(cfg):
    """The relay writes its refusals for people -- "That is not the word shown
    on the computer you are linking". Replacing them with a generic message
    here would throw away the only sentence that tells the user what happened."""
    respx.post(f"{RELAY}/pair/poll").mock(
        return_value=httpx.Response(404, json={"detail": "That pairing has expired."})
    )
    with pytest.raises(RelayError) as e:
        PairingClient(cfg).poll("c", "s")
    assert "expired" in str(e.value)


@respx.mock
def test_a_non_json_refusal_still_produces_a_sentence(cfg):
    """nginx serves HTML for 413 and 502, which will not parse. A client that
    assumed JSON would raise a parse error on top of the real one."""
    respx.post(f"{RELAY}/pair/start").mock(
        return_value=httpx.Response(502, text="<html>Bad Gateway</html>")
    )
    with pytest.raises(RelayError) as e:
        PairingClient(cfg).start("m", "p")
    assert "try again" in str(e.value).lower()


@respx.mock
def test_hello_reports_the_presets_this_machine_can_run(cfg):
    """This call is how *World Class* appears in a browser that would otherwise
    only be offered the shared worker's three."""
    route = respx.post(f"{RELAY}/worker/hello").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    WorkerClient(cfg, "tok").hello("Mac", "1.0.0", "bgsage 2.0", ["world_class", "deep"])
    assert b"world_class" in route.calls.last.request.read()
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"


@respx.mock
def test_an_empty_poll_is_a_job_of_none(cfg):
    """The relay answers `{"job": null}` rather than a 204 so the loop has one
    response shape to parse and no status-code branch."""
    respx.get(f"{RELAY}/worker/next-job").mock(
        return_value=httpx.Response(200, json={"job": None})
    )
    assert WorkerClient(cfg, "tok").next_job() is None


@respx.mock
def test_a_claimed_job_arrives_base64_and_comes_back_as_bytes(cfg):
    """The handout is JSON because it carries the preset too, so the match is
    base64 in it; the result goes back the other way as a raw body."""
    respx.get(f"{RELAY}/worker/next-job").mock(
        return_value=httpx.Response(
            200,
            json={"job": {"id": "j1", "preset": "world_class",
                          "match": base64.b64encode(b"OGXM-bytes").decode(),
                          "lease_seconds": 300}},
        )
    )
    job = WorkerClient(cfg, "tok").next_job()
    assert job.id == "j1"
    assert job.preset == "world_class"
    assert job.match_bytes == b"OGXM-bytes"


@respx.mock
def test_the_result_goes_up_as_a_raw_body(cfg):
    """Not multipart and not JSON: this direction carries bytes and nothing
    else, and base64ing a 4 MB result to match the handout's shape would cost a
    third of it for nothing."""
    route = respx.post(f"{RELAY}/worker/jobs/j1/result").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    WorkerClient(cfg, "tok").deliver("j1", b"\x1f\x8bgzipped")
    assert route.calls.last.request.read() == b"\x1f\x8bgzipped"
    assert route.calls.last.request.headers["content-type"] == "application/octet-stream"


@respx.mock
def test_progress_carries_both_counts(cfg):
    """Both, because the relay stores both and the browser draws a bar from the
    pair. Sending `done` alone would leave the bar with no denominator."""
    route = respx.post(f"{RELAY}/worker/jobs/j1/progress").mock(
        return_value=httpx.Response(200, json={"status": "ok", "lease_seconds": 300})
    )
    WorkerClient(cfg, "tok").progress("j1", 7, 42)
    body = route.calls.last.request.read()
    assert b'"decisions_done":7' in body
    assert b'"decisions_total":42' in body


@respx.mock
def test_a_failure_message_is_truncated_to_what_the_relay_accepts(cfg):
    """`ErrorBody.error` is `max_length=500`. A traceback longer than that would
    be a 422 -- which would turn a reportable analysis failure into a job that
    hangs until its lease expires."""
    route = respx.post(f"{RELAY}/worker/jobs/j1/error").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    WorkerClient(cfg, "tok").fail("j1", "x" * 5000)
    import json
    assert len(json.loads(route.calls.last.request.read())["error"]) == 500


@respx.mock
def test_a_401_is_its_own_exception(cfg):
    """The one error the daemon must not retry: it means this machine was
    unlinked, which will still be true tomorrow. Every other failure is worth
    another poll in thirty seconds."""
    respx.get(f"{RELAY}/worker/next-job").mock(return_value=httpx.Response(401))
    with pytest.raises(Unauthorized):
        WorkerClient(cfg, "tok").next_job()


@respx.mock
def test_a_500_is_retryable_rather_than_fatal(cfg):
    """Asserted as *not* Unauthorized, because the distinction is what decides
    whether the helper keeps running."""
    respx.get(f"{RELAY}/worker/next-job").mock(return_value=httpx.Response(500))
    with pytest.raises(RelayError) as e:
        WorkerClient(cfg, "tok").next_job()
    assert not isinstance(e.value, Unauthorized)


@respx.mock
def test_the_poll_asks_the_relay_to_hold_the_request(cfg):
    """A long poll, not a busy loop. Without `wait` the relay answers at once
    and the helper would poll the VPS as fast as the network allows."""
    route = respx.get(f"{RELAY}/worker/next-job").mock(
        return_value=httpx.Response(200, json={"job": None})
    )
    WorkerClient(cfg, "tok").next_job()
    assert route.calls.last.request.url.params["wait"] == str(POLL_WAIT)


@respx.mock
def test_the_poll_stays_under_the_commonest_proxy_timeout(cfg):
    """The number itself is the assertion.

    A poll held for as long as the proxy will wait is a coin flip between an
    answer and a 504, which is what beta did on 2026-09-16 at roughly every
    other poll. 30 is the read timeout to beat, and the helper crosses proxies
    nobody here controls -- so the margin lives on this side rather than in one
    nginx config.
    """
    assert POLL_WAIT < 30


@respx.mock
def test_a_proxy_timeout_is_an_empty_poll_not_a_failure(cfg, monkeypatch):
    """504 is a gateway's impatience, not the relay's refusal.

    Nothing was claimed, so nothing is lost by asking again -- and treating it
    as an error is what turned a proxy timeout into a backoff, and a backoff
    into a helper asleep when the work arrived.
    """
    # The floor is shortened here rather than waited out: five statuses times
    # five seconds is half a minute of CI spent proving nothing this test is
    # about. `test_an_instant_gateway_error_still_costs_a_wait` is where the
    # waiting itself is asserted.
    monkeypatch.setattr(client_module, "POLL_GATEWAY_FLOOR", 0.01)
    for status in (502, 503, 504, 408, 524):
        respx.get(f"{RELAY}/worker/next-job").mock(
            return_value=httpx.Response(status, html="<h1>Gateway Time-out</h1>")
        )
        assert WorkerClient(cfg, "tok").next_job() is None, status


@respx.mock
def test_an_instant_gateway_error_still_costs_a_wait(cfg, monkeypatch):
    """The sharp edge of the rule above.

    A proxy whose upstream is *down* answers in milliseconds, so a poll meant to
    park for twenty seconds returns at once and the loop asks again -- a busy
    loop against a service already having a bad morning. A real read timeout has
    already spent its thirty seconds, so this floor is free in the case it is
    actually for.
    """
    monkeypatch.setattr(client_module, "POLL_GATEWAY_FLOOR", 0.4)
    respx.get(f"{RELAY}/worker/next-job").mock(return_value=httpx.Response(502))
    started = time.monotonic()
    assert WorkerClient(cfg, "tok").next_job() is None
    assert time.monotonic() - started >= 0.3

    # And the shipped value is a real one, not a token gesture.
    assert POLL_GATEWAY_FLOOR >= 1.0

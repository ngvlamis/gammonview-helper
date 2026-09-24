# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Does a *live* relay still speak the protocol this client implements?

`test_client.py` tests the same contract against `respx`, which is a recording
of what the relay did when the test was written. That catches a change on this
side and is blind to a change on the other -- and the other side lives in a
private repository that this one cannot see, cannot import, and cannot run in
CI. `docs/Relay.md` explains why that split is deliberate and what it costs.

This is the part that costs. These tests need a reachable relay, so they are
opt-in:

    GAMMONVIEW_CONTRACT_BASE=https://beta.gammonview.com uv run pytest -m contract

They assert the *shape* of the seam, never an analysis: a pairing is started
and then abandoned unconfirmed, which the relay forgets after sixty seconds.
Nothing here needs a credential, and nothing here can obtain one -- confirming
a pairing takes a browser and a person, which is the whole design.
"""

from __future__ import annotations

import os

import httpx
import pytest

from gvhelper.client import RELAY_CONTRACT, CLIENT_AGENT

pytestmark = pytest.mark.contract

BASE = os.environ.get("GAMMONVIEW_CONTRACT_BASE")

requires_relay = pytest.mark.skipif(
    not BASE, reason="set GAMMONVIEW_CONTRACT_BASE to a reachable GammonView site"
)


@pytest.fixture
def relay():
    return f"{BASE.rstrip('/')}/accounts/relay"


@pytest.fixture
def client():
    with httpx.Client(timeout=30.0, headers={"User-Agent": CLIENT_AGENT}) as c:
        yield c


@requires_relay
def test_the_site_is_up(client):
    r = client.get(f"{BASE.rstrip('/')}/accounts/health")
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "ok"


@requires_relay
def test_pairing_start_returns_two_distinct_secrets_and_a_word(client, relay):
    """The one call this suite is allowed to make for real.

    It asserts the thing most worth asserting: that `code` and `secret` come
    back as *different* strings. A relay refactor that collapsed them into one
    would pass every mocked test in `test_client.py` -- they mock what the
    relay returns -- and would hand anyone who saw a pairing link the token it
    was about to mint.
    """
    r = client.post(
        f"{relay}/pair/start",
        json={"machine": "contract-test", "platform": "pytest"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["code"] and body["secret"]
    assert body["code"] != body["secret"]
    # Three words, one of which the helper prints and the browser must pick.
    assert len(body["choices"]) == 3
    assert body["word"] in body["choices"]
    # The screen has a minute to answer. `PairingClient.poll` is built around
    # this number, so a change to it belongs in a contract revision.
    assert body["expires_in"] == 60


@requires_relay
def test_an_over_long_wait_is_clamped_rather_than_refused(client, relay):
    """Revision 2's compatibility promise, checked against the deployment.

    Unauthenticated, so this never reaches the poll itself -- a 422 comes from
    the query validator *before* the dependency runs, so 401 here is proof the
    request got past validation. That is the whole assertion: a helper asking
    for the old 30-second maximum must not be answered with a 422 because the
    server lowered its cap.
    """
    r = client.get(f"{relay}/worker/next-job", params={"wait": 30})
    assert r.status_code != 422, "the wait bound is validated, not clamped"
    assert r.status_code in (401, 403)


@requires_relay
def test_the_worker_routes_exist_and_refuse_an_anonymous_caller(client, relay):
    """Every worker route, asked without a token.

    401 is the pass. What this is really looking for is **404**, which is what
    a renamed or removed route returns -- and which a paired helper experiences
    as polling forever and never being given work.
    """
    routes = [
        ("POST", f"{relay}/worker/hello"),
        ("GET", f"{relay}/worker/next-job"),
        ("POST", f"{relay}/worker/jobs/fake/progress"),
        ("POST", f"{relay}/worker/jobs/fake/result"),
        ("POST", f"{relay}/worker/jobs/fake/error"),
        # The one worker route that is not about a job, and the only one whose
        # path is the router prefix itself -- which is what makes it worth
        # listing here: it has no path segment of its own to make it visible in
        # a route table, so it is the easiest of these to lose to a refactor of
        # the prefix. Verified against the service: while it exists, other
        # methods on that path answer 405; remove it and this DELETE answers
        # 404, which is the assertion below.
        ("DELETE", f"{relay}/worker"),
    ]
    for method, url in routes:
        r = client.request(method, url)
        assert r.status_code != 404, f"{method} {url} is gone (relay contract {RELAY_CONTRACT})"
        assert r.status_code in (401, 403), f"{method} {url} -> {r.status_code}"

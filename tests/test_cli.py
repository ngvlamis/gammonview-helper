# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The four commands, as a person meets them.

These assert on what is printed as much as on what is returned, because the
audience is somebody who has been talked into opening a terminal: a correct exit
code they cannot see is worth nothing next to a sentence telling them what to do.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from gvhelper import cli, store
from gvhelper.config import Config

RELAY = "https://example.test/accounts/relay"


@pytest.fixture(autouse=True)
def _no_browser_no_engine(monkeypatch):
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(cli, "engine_version", lambda: "bgsage test")
    monkeypatch.setattr(cli, "available_presets", lambda: ["fast", "world_class"])
    cli._opened = opened
    return opened


def _run(argv):
    return cli.main(["--site", "https://example.test"] + argv)


@respx.mock
def test_linking_stores_a_token_and_says_where(capsys):
    respx.post(f"{RELAY}/pair/start").mock(
        return_value=httpx.Response(200, json={
            "code": "c1", "secret": "s1", "word": "HERON",
            "choices": ["HERON", "ANVIL", "MAPLE"], "expires_in": 60,
        })
    )
    respx.post(f"{RELAY}/pair/poll").mock(
        return_value=httpx.Response(200, json={"status": "linked", "token": "wt", "worker_id": "w9"})
    )
    assert _run(["link"]) == 0
    out = capsys.readouterr().out
    assert "HERON" in out
    assert store.load_token(Config(site="https://example.test")) == "wt"


@respx.mock
def test_the_word_is_shown_where_it_cannot_be_skimmed_past(capsys):
    """The security-critical line of the whole install. A user who skims it is a
    user who will guess at the three words in the browser, which is exactly the
    attack the three words exist to stop."""
    respx.post(f"{RELAY}/pair/start").mock(
        return_value=httpx.Response(200, json={
            "code": "c1", "secret": "s1", "word": "HERON", "choices": [], "expires_in": 60,
        })
    )
    respx.post(f"{RELAY}/pair/poll").mock(
        return_value=httpx.Response(200, json={"status": "linked", "token": "t", "worker_id": "w"})
    )
    _run(["link"])
    lines = capsys.readouterr().out.splitlines()
    boxed = [i for i, line in enumerate(lines) if "HERON" in line]
    assert boxed, "the word was never printed"
    # Drawn in a box: the lines above and below it are rule characters.
    assert "─" in lines[boxed[0] - 1]
    assert "─" in lines[boxed[0] + 1]


@respx.mock
def test_the_browser_is_sent_to_the_confirmation_route():
    respx.post(f"{RELAY}/pair/start").mock(
        return_value=httpx.Response(200, json={
            "code": "c1", "secret": "s1", "word": "W", "choices": [], "expires_in": 60,
        })
    )
    respx.post(f"{RELAY}/pair/poll").mock(
        return_value=httpx.Response(200, json={"status": "linked", "token": "t", "worker_id": "w"})
    )
    _run(["link"])
    assert cli._opened == ["https://example.test/#/link-helper?code=c1"]


@respx.mock
def test_no_browser_still_prints_the_link(capsys):
    """For a machine with no browser -- a NAS, a headless box, a remote shell --
    which is a real way people would run this."""
    respx.post(f"{RELAY}/pair/start").mock(
        return_value=httpx.Response(200, json={
            "code": "c1", "secret": "s1", "word": "W", "choices": [], "expires_in": 60,
        })
    )
    respx.post(f"{RELAY}/pair/poll").mock(
        return_value=httpx.Response(200, json={"status": "linked", "token": "t", "worker_id": "w"})
    )
    _run(["link", "--no-browser"])
    assert cli._opened == []
    assert "https://example.test/#/link-helper?code=c1" in capsys.readouterr().out


@respx.mock
def test_the_wrong_word_leaves_nothing_stored(capsys):
    """The relay deletes the pairing when somebody picks wrong. The helper must
    not keep a half-linked state around afterwards, and must say that starting
    again is one command."""
    respx.post(f"{RELAY}/pair/start").mock(
        return_value=httpx.Response(200, json={
            "code": "c1", "secret": "s1", "word": "HERON", "choices": [], "expires_in": 60,
        })
    )
    respx.post(f"{RELAY}/pair/poll").mock(
        return_value=httpx.Response(404, json={"detail": "no such pairing"})
    )
    assert _run(["link"]) == 1
    assert store.load_token(Config(site="https://example.test")) is None
    assert "link" in capsys.readouterr().out


@respx.mock
def test_nobody_confirming_says_so_rather_than_hanging(capsys, monkeypatch):
    """A helper that polled forever would leave a word on screen and no reason
    to believe anything was wrong."""
    respx.post(f"{RELAY}/pair/start").mock(
        return_value=httpx.Response(200, json={
            "code": "c1", "secret": "s1", "word": "W", "choices": [], "expires_in": 0,
        })
    )
    respx.post(f"{RELAY}/pair/poll").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    # Expire the window immediately rather than waiting the real five seconds.
    times = iter([0.0, 100.0, 200.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(times))
    assert _run(["link"]) == 1
    assert "in time" in capsys.readouterr().out


def test_running_unlinked_once_says_what_to_do_first(capsys):
    """`--once` still exits, and must: it is what a "does this work" check and
    the tests use, and neither can block for a credential that is never
    coming. Without the flag the helper now waits instead -- see
    `test_daemon.py`, and `cmd_run` for why exiting was wrong."""
    assert _run(["run", "--once"]) == 2
    assert "link" in capsys.readouterr().out


@respx.mock
def test_status_checks_the_link_rather_than_only_the_file(capsys):
    """Every interesting way this breaks is invisible locally: a token revoked
    in account settings, an account deleted. Local state looks perfect in both."""
    store.save_token(Config(site="https://example.test"), "tok")
    respx.post(f"{RELAY}/worker/hello").mock(return_value=httpx.Response(401))
    assert _run(["status"]) == 1
    assert "no longer linked" in capsys.readouterr().out


@respx.mock
def test_status_reports_a_working_link(capsys):
    store.save_token(Config(site="https://example.test"), "tok")
    respx.post(f"{RELAY}/worker/hello").mock(return_value=httpx.Response(200, json={}))
    assert _run(["status"]) == 0
    out = capsys.readouterr().out
    assert "working" in out
    assert "world_class" in out


def test_status_unlinked_needs_no_network(capsys):
    """Asserted by making no respx mock at all: a stray request would raise."""
    assert _run(["status"]) == 1
    assert "linked     : no" in capsys.readouterr().out


def test_unlink_is_honest_about_being_local_only(capsys):
    """The helper holds a worker-scoped token, which by design cannot reach the
    account routes -- so it cannot remove its own row from settings. Saying
    otherwise would leave someone believing they had revoked something they had
    not."""
    cfg = Config(site="https://example.test")
    store.save_token(cfg, "tok")
    assert _run(["unlink"]) == 0
    assert store.load_token(cfg) is None
    out = capsys.readouterr().out
    assert "account settings" in out


def test_no_command_prints_help(capsys):
    assert cli.main([]) == 1
    assert "usage" in capsys.readouterr().out.lower()

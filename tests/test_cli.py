# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The four commands, as a person meets them.

These assert on what is printed as much as on what is returned, because the
audience is somebody who has been talked into opening a terminal: a correct exit
code they cannot see is worth nothing next to a sentence telling them what to do.
"""

from __future__ import annotations

import json

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


# --- `link --porcelain`, which is the installer's half of the device flow ------
#
# The launcher is a frozen native binary: it ships once, is never upgraded in
# place, and cannot be fixed after the fact. So what it reads has to be a
# contract rather than whatever the prose happened to say that month, and these
# assert the three properties it actually rests on -- machine-readable, exactly
# one terminal event on every path, and ASCII on the wire.


def _events(out):
    """Every line of porcelain output, parsed. A line that is not JSON is the
    failure this whole format exists to prevent, so it raises here rather than
    being filtered out."""
    return [json.loads(line) for line in out.splitlines() if line.strip()]


@respx.mock
def test_porcelain_gives_the_installer_the_word_without_prose(capsys):
    """The word has to reach a native window. Scraping it out of `_banner`'s box
    would make rewording a sentence a bug in software already installed."""
    respx.post(f"{RELAY}/pair/start").mock(
        return_value=httpx.Response(200, json={
            "code": "c1", "secret": "s1", "word": "HERON",
            "choices": ["HERON", "ANVIL", "MAPLE"], "expires_in": 60,
        })
    )
    respx.post(f"{RELAY}/pair/poll").mock(
        return_value=httpx.Response(200, json={"status": "linked", "token": "wt", "worker_id": "w9"})
    )
    assert _run(["link", "--porcelain", "--name", "Studio"]) == 0
    events = _events(capsys.readouterr().out)
    assert [e["event"] for e in events] == ["offer", "linked"]
    offer, linked = events
    assert offer["word"] == "HERON"
    assert offer["url"] == "https://example.test/#/link-helper?code=c1"
    assert offer["machine"] == "Studio"
    assert offer["expires_in"] == 60
    assert linked["worker_id"] == "w9"
    assert store.load_token(Config(site="https://example.test")) == "wt"


@respx.mock
def test_porcelain_says_nothing_a_reader_cannot_parse(capsys):
    """Not one stray `print`. A progress window that hits a line of prose has no
    way to tell a status update from a failure."""
    respx.post(f"{RELAY}/pair/start").mock(
        return_value=httpx.Response(200, json={
            "code": "c1", "secret": "s1", "word": "HERON", "choices": [], "expires_in": 60,
        })
    )
    respx.post(f"{RELAY}/pair/poll").mock(
        return_value=httpx.Response(200, json={"status": "linked", "token": "t", "worker_id": "w"})
    )
    _run(["link", "--porcelain"])
    for line in capsys.readouterr().out.splitlines():
        if line.strip():
            json.loads(line)  # raises, loudly, on anything else


@respx.mock
def test_porcelain_is_ascii_even_when_the_machine_is_not(capsys):
    """`json.dumps` escapes non-ASCII by default and that default is kept on
    purpose: a Windows console on a legacy code page turns an unescaped `o with
    an umlaut` into a `UnicodeEncodeError`, and the installer would see the flow
    die at the one moment it is holding the user's attention."""
    respx.post(f"{RELAY}/pair/start").mock(
        return_value=httpx.Response(200, json={
            "code": "c1", "secret": "s1", "word": "HERON", "choices": [], "expires_in": 60,
        })
    )
    respx.post(f"{RELAY}/pair/poll").mock(
        return_value=httpx.Response(200, json={"status": "linked", "token": "t", "worker_id": "w"})
    )
    assert _run(["link", "--porcelain", "--name", "Bj\u00f6rns MacBook"]) == 0
    out = capsys.readouterr().out
    out.encode("ascii")  # raises if anything went out raw
    assert _events(out)[0]["machine"] == "Bj\u00f6rns MacBook"


@respx.mock
def test_porcelain_cannot_start_ends_in_one_event(capsys):
    respx.post(f"{RELAY}/pair/start").mock(side_effect=httpx.ConnectError("no route"))
    assert _run(["link", "--porcelain"]) == 1
    events = _events(capsys.readouterr().out)
    assert [e["event"] for e in events] == ["failed"]
    # Nothing to retry: the site is unreachable, and an installer that offers
    # "try again" here gets pressed four times.
    assert events[0]["retry"] is False
    assert events[0]["reason"]


@respx.mock
def test_porcelain_wrong_word_ends_in_one_event(capsys):
    respx.post(f"{RELAY}/pair/start").mock(
        return_value=httpx.Response(200, json={
            "code": "c1", "secret": "s1", "word": "HERON", "choices": [], "expires_in": 60,
        })
    )
    respx.post(f"{RELAY}/pair/poll").mock(
        return_value=httpx.Response(404, json={"detail": "no such pairing"})
    )
    assert _run(["link", "--porcelain"]) == 1
    events = _events(capsys.readouterr().out)
    assert [e["event"] for e in events] == ["offer", "failed"]
    assert events[-1]["retry"] is True
    assert store.load_token(Config(site="https://example.test")) is None


@respx.mock
def test_porcelain_nobody_confirms_ends_in_one_event(capsys, monkeypatch):
    """The path where silence would otherwise be the only signal, which is
    exactly what a reader must never have to interpret."""
    respx.post(f"{RELAY}/pair/start").mock(
        return_value=httpx.Response(200, json={
            "code": "c1", "secret": "s1", "word": "W", "choices": [], "expires_in": 0,
        })
    )
    respx.post(f"{RELAY}/pair/poll").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    times = iter([0.0, 100.0, 200.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(times))
    assert _run(["link", "--porcelain"]) == 1
    events = _events(capsys.readouterr().out)
    assert [e["event"] for e in events] == ["offer", "failed"]
    assert events[-1]["retry"] is True


@respx.mock
def test_porcelain_leaves_the_browser_to_the_installer(capsys):
    """The launcher passes `--no-browser` and opens the URL after its own window
    is showing the word -- a browser that steals focus first is a browser the
    user reads before they have read the word they are supposed to match."""
    respx.post(f"{RELAY}/pair/start").mock(
        return_value=httpx.Response(200, json={
            "code": "c1", "secret": "s1", "word": "W", "choices": [], "expires_in": 60,
        })
    )
    respx.post(f"{RELAY}/pair/poll").mock(
        return_value=httpx.Response(200, json={"status": "linked", "token": "t", "worker_id": "w"})
    )
    _run(["link", "--porcelain", "--no-browser"])
    assert cli._opened == []
    # The URL is still handed over -- in a field, not in a sentence.
    assert _events(capsys.readouterr().out)[0]["url"].endswith("?code=c1")


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


@respx.mock
def test_status_with_no_network_diagnoses_instead_of_crashing(capsys):
    """`status` is what somebody runs *because* something is wrong, so the one
    state it must not fail in is the one where the network is the problem."""
    store.save_token(Config(site="https://example.test"), "tok")
    respx.post(f"{RELAY}/worker/hello").mock(side_effect=httpx.ConnectError("nope"))
    assert _run(["status"]) == 1
    out = capsys.readouterr().out
    assert "could not reach the relay" in out
    assert "Traceback" not in out
    # The rest of the report is still there: which site, which store, which
    # engine -- all of it local, all of it the part worth reading offline.
    assert "site       : https://example.test/accounts" in out


@respx.mock
def test_status_porcelain_answers_the_launcher_s_one_question(capsys):
    """Is this machine already linked? Pairing again when it is leaves a second
    worker registered against the account, listed twice in settings with no way
    to tell which is real."""
    store.save_token(Config(site="https://example.test"), "tok")
    respx.post(f"{RELAY}/worker/hello").mock(return_value=httpx.Response(200, json={}))
    assert _run(["status", "--porcelain"]) == 0
    events = _events(capsys.readouterr().out)
    assert len(events) == 1, "one object, so a reader never has to choose"
    assert events[0]["event"] == "status"
    assert events[0]["linked"] is True
    assert events[0]["link"] == "working"
    assert events[0]["site"] == "https://example.test/accounts"


def test_status_porcelain_unlinked_needs_no_network(capsys):
    """The state a fresh install is in, and the reason the launcher is about to
    run `link`. It must not be reported by failing to reach anything."""
    assert _run(["status", "--porcelain"]) == 1
    event = _events(capsys.readouterr().out)[0]
    assert event["linked"] is False
    assert event["link"] is None


@respx.mock
def test_status_porcelain_tells_revoked_from_unreachable(capsys):
    """Two failures a launcher must not confuse: one means link again, the
    other means the wifi is off and nothing is wrong with the account."""
    store.save_token(Config(site="https://example.test"), "tok")
    respx.post(f"{RELAY}/worker/hello").mock(return_value=httpx.Response(401))
    assert _run(["status", "--porcelain"]) == 1
    assert _events(capsys.readouterr().out)[0]["link"] == "revoked"

    respx.post(f"{RELAY}/worker/hello").mock(side_effect=httpx.ConnectError("nope"))
    assert _run(["status", "--porcelain"]) == 1
    event = _events(capsys.readouterr().out)[0]
    assert event["link"] == "unreachable"
    # Still linked: the credential is fine, the network is not.
    assert event["linked"] is True


@respx.mock
def test_status_porcelain_says_nothing_else(capsys):
    """The aligned report and the JSON are alternatives, not a JSON line
    appended to a report."""
    store.save_token(Config(site="https://example.test"), "tok")
    respx.post(f"{RELAY}/worker/hello").mock(return_value=httpx.Response(200, json={}))
    _run(["status", "--porcelain"])
    for line in capsys.readouterr().out.splitlines():
        if line.strip():
            json.loads(line)


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

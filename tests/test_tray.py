# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The menu-bar item's words and decisions -- everything but AppKit itself.

The menu cannot be drawn in a test, so what is tested is what it would say and
when it would act: the status line, and whether Quit may touch launchd.
"""

from __future__ import annotations

from gvhelper import tray
from gvhelper.daemon import Control
from gvhelper.runner import Progress


def test_the_status_line_follows_the_loop():
    c = Control()
    c.state = "unlinked"
    assert tray.status_line(c) == "Not linked to an account"
    c.state = "idle"
    assert tray.status_line(c) == "Ready for matches"
    c.state = "busy"
    c.progress = Progress()
    c.progress.set(37, 100)
    assert tray.status_line(c) == "Analysing a match… 37%"


def test_pausing_mid_match_says_the_match_will_finish():
    c = Control()
    c.state = "busy"
    c.progress = Progress()
    c.progress.set(1, 4)
    c.pause()
    assert tray.status_line(c) == "Pausing after this match… 25%"
    c.progress = None
    assert tray.status_line(c) == "Paused"


def test_quit_only_boots_out_the_login_item_it_is(monkeypatch):
    """A helper started in a terminal must not take down the login item."""
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    assert not tray.under_login_item()
    monkeypatch.setenv("XPC_SERVICE_NAME", tray.LAUNCHD_LABEL)
    assert tray.under_login_item()


def test_no_menu_over_ssh(monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 1 10.0.0.2 22")
    assert not tray.available()


def test_the_icons_ship_with_the_package():
    from pathlib import Path

    for paused in (False, True):
        assert Path(tray._icon(paused)).is_file()

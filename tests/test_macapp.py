# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The app in Applications: what it is made of, and what opening it does.

Nothing here touches the real `/Applications` or launchd: the folders are
redirected into `tmp_path`, and `start` is handed a fake `launchctl`.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys

import pytest

from gvhelper import macapp
from gvhelper.cli import main

# macOS-only code, but tested everywhere it can be: only Windows lacks the
# shell, the `getuid` and the permission bits these tests lean on.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="macOS app bundle")


@pytest.fixture
def folders(tmp_path, monkeypatch):
    system, home = tmp_path / "Applications", tmp_path / "home" / "Applications"
    system.mkdir()
    monkeypatch.setattr(macapp, "locations", lambda: [system, home])
    return system, home


def test_the_app_is_written_with_the_helper_behind_it(folders):
    app = macapp.ensure("/opt/gv/bin/gammonview-helper", "0.4.0")
    assert app == folders[0] / macapp.APP_NAME
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "start"
    assert info["CFBundleShortVersionString"] == "0.4.0"
    script = app / "Contents" / "MacOS" / "start"
    assert 'HELPER="/opt/gv/bin/gammonview-helper"' in script.read_text()
    assert script.stat().st_mode & 0o111
    assert (app / "Contents" / "Resources" / "AppIcon.icns").stat().st_size > 0


def test_an_unchanged_app_is_left_alone(folders):
    app = macapp.ensure("/opt/gv/bin/gammonview-helper", "0.4.0")
    script = app / "Contents" / "MacOS" / "start"
    before = script.stat().st_mtime_ns
    macapp.ensure("/opt/gv/bin/gammonview-helper", "0.4.0")
    assert script.stat().st_mtime_ns == before
    macapp.ensure("/opt/gv/bin/gammonview-helper", "0.5.0")
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleVersion"] == "0.5.0"


def test_a_user_who_cannot_write_applications_gets_their_own(folders, monkeypatch):
    system, home = folders
    system.chmod(0o555)
    try:
        assert macapp.ensure("/x", "1") == home / macapp.APP_NAME
    finally:
        system.chmod(0o755)


def test_the_app_removes_itself_once_the_helper_is_gone(folders, tmp_path):
    app = macapp.ensure(str(tmp_path / "no-such-helper"), "1")
    subprocess.run([str(app / "Contents" / "MacOS" / "start")], check=True)
    assert not app.exists()


def test_the_installers_remove_takes_the_app_with_it(folders, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    app = macapp.ensure("/x", "1")
    assert main(["unlink", "--local-only", "--porcelain"]) == 0
    assert not app.exists()


class FakeLaunchctl:
    def __init__(self, loaded: bool, running: bool = False):
        self.loaded, self.running, self.calls = loaded, running, []

    def __call__(self, argv):
        self.calls.append(argv[1])
        out = "state = running" if self.running else "state = not running"
        return subprocess.CompletedProcess(argv, 0 if self.loaded else 113, out, "")


@pytest.fixture
def plist(tmp_path, monkeypatch):
    path = tmp_path / "com.gammonview.helper.plist"
    path.write_text("<plist/>")
    monkeypatch.setattr(macapp, "plist_path", lambda: path)
    return path


def test_opening_after_quit_loads_the_login_item_again(plist):
    launchctl = FakeLaunchctl(loaded=False)
    assert macapp.start(launchctl) is None
    assert launchctl.calls == ["print", "bootstrap"]


def test_opening_between_restarts_kicks_it(plist):
    launchctl = FakeLaunchctl(loaded=True, running=False)
    assert macapp.start(launchctl) is None
    assert launchctl.calls == ["print", "kickstart"]


def test_opening_while_running_says_where_to_look(plist):
    launchctl = FakeLaunchctl(loaded=True, running=True)
    assert macapp.start(launchctl) == macapp.ALREADY_RUNNING
    assert launchctl.calls == ["print"]


def test_opening_with_no_login_item_says_how_to_get_one(plist):
    plist.unlink()
    launchctl = FakeLaunchctl(loaded=False)
    assert macapp.start(launchctl) == macapp.NOT_SET_UP
    assert launchctl.calls == []

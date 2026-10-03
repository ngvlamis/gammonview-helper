# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The app in Applications: something to open, as with Dropbox.

The helper is a login item, so somebody who chooses Quit from its menu has
stopped it until they next log in -- and before this, the only ways to start it
sooner were the installer's Update button, which says nothing about starting
anything, and `launchctl`. Dropbox and Google Drive answer the same problem with
an app you open, and so does this.

**The helper writes the app itself, on the machine it runs on.** Gatekeeper
checks what was *downloaded*: a file carries the quarantine mark only if a
browser or similar put it there, and a bundle created locally by a process
already running is opened with no dialog at all, unsigned or not. So the app
costs nobody a Gatekeeper prompt, needs no change to the frozen launcher, and
reaches every machine the installer ever set up on its next update.

**The app is a three-line shell script and an icon.** Everything it decides is
`gammonview-helper start`, which updates through PyPI like the rest; the script
only finds the helper, and removes the app when the helper is gone.

**Uninstall.** The installer's Remove deletes the install root and the login
item and knows nothing about this. Two things cover that: `unlink --porcelain`,
which only the installer's Remove runs, deletes the app; and the script deletes
its own bundle when opened with no helper behind it, for a machine where
neither happened.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path
from typing import Callable

from .tray import LAUNCHD_LABEL

APP_NAME = "GammonView Helper.app"
BUNDLE_ID = "com.gammonview.helper.app"


def locations() -> list[Path]:
    """Where the app may be, in order of preference.

    `/Applications` first: it is the folder in Finder's sidebar, and where
    Dropbox and Google Drive put theirs. Most Mac users are admins and can
    write to it without a password; one who cannot gets `~/Applications`, which
    Spotlight and Launchpad search all the same.
    """
    return [Path("/Applications"), Path.home() / "Applications"]


def installed() -> Path | None:
    for folder in locations():
        app = folder / APP_NAME
        if app.exists():
            return app
    return None


def _target() -> Path:
    existing = installed()
    if existing is not None:
        return existing
    for folder in locations():
        if folder.is_dir() and os.access(folder, os.W_OK):
            return folder / APP_NAME
    return locations()[-1] / APP_NAME


def info_plist(version: str) -> bytes:
    return plistlib.dumps(
        {
            "CFBundleExecutable": "start",
            "CFBundleIdentifier": BUNDLE_ID,
            "CFBundleName": "GammonView Helper",
            "CFBundleDisplayName": "GammonView Helper",
            "CFBundleIconFile": "AppIcon",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": version,
            "CFBundleVersion": version,
            # No Dock icon of its own: the script is gone in a second, and what
            # stays is the menu-bar item.
            "LSUIElement": True,
        }
    )


def launcher_script(helper: str) -> str:
    quoted = helper.replace('"', '\\"')
    return f"""#!/bin/sh
# Written by gammonview-helper; rewritten on every update. See gvhelper/macapp.py.
HELPER="{quoted}"
if [ ! -x "$HELPER" ]; then
    # The helper has been uninstalled. An app that opens nothing is clutter.
    rm -rf "$(cd "$(dirname "$0")/../.." && pwd)"
    exit 0
fi
exec "$HELPER" start
"""


def ensure(helper: str, version: str) -> Path | None:
    """Write or refresh the app, and return where it is.

    Run by the login item on each start, so an update reaches it. Files are
    rewritten only when they differ, so an unchanged app keeps its dates and
    Finder has nothing to re-read. Never raises: a missing app is the state
    every machine was in before this existed, and no reason to stop analysing.
    """
    app = _target()
    contents = app / "Contents"
    wanted: list[tuple[Path, bytes, int]] = [
        (contents / "Info.plist", info_plist(version), 0o644),
        (contents / "MacOS" / "start", launcher_script(helper).encode(), 0o755),
        (
            contents / "Resources" / "AppIcon.icns",
            (files("gvhelper") / "resources" / "AppIcon.icns").read_bytes(),
            0o644,
        ),
    ]
    try:
        for path, data, mode in wanted:
            if path.exists() and path.read_bytes() == data:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(mode)
    except OSError as e:
        print(f"could not write {app}: {e}", flush=True)
        return None
    return app


def remove() -> None:
    for folder in locations():
        shutil.rmtree(folder / APP_NAME, ignore_errors=True)


# --- what opening the app does ----------------------------------------------

#: Spoken by `start` when there is nothing to start. Titles and messages.
ALREADY_RUNNING = (
    "GammonView Helper is already running.",
    "Look for the cube in the menu bar at the top right of your screen.",
)
NOT_SET_UP = (
    "GammonView Helper is not set up on this computer.",
    "To set it up again, download the installer from gammonview.com.",
)


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def start(run: Callable[[list[str]], subprocess.CompletedProcess] | None = None):
    """Start the login item, and return what to tell the person, if anything.

    `None` means it was started: the cube appearing in the menu bar is the
    answer, as it is for Dropbox. Three launchd states, three actions:

    * not loaded -- what Quit leaves -- is `bootstrap`, which loads the plist
      and, since it is `RunAtLoad`, starts it;
    * loaded and not running (between `KeepAlive` restarts) is `kickstart`;
    * running needs nothing, and saying so is the whole point of opening it.
    """
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    plist = plist_path()
    if not plist.exists():
        return NOT_SET_UP
    service = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
    printed = run(["launchctl", "print", service])
    if printed.returncode != 0:
        run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])
        return None
    if "state = running" in printed.stdout:
        return ALREADY_RUNNING
    run(["launchctl", "kickstart", service])
    return None


def tell(message: tuple[str, str]) -> None:
    """Show `message` in a dialog with the app's icon."""
    import rumps
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    app.activateIgnoringOtherApps_(True)
    rumps.alert(
        title=message[0],
        message=message[1],
        icon_path=str(files("gvhelper") / "resources" / "AppIcon.icns"),
    )

# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The menu-bar item: what the helper is doing, and Pause, Link and Quit.

macOS only, for now. The Mac is the one platform with an installer and a login
item, which makes it the one place the helper runs with no terminal: without
this, the only way to stop it is `launchctl`, and the only way to tell whether
it is working is the website.

**It lives here and not in the launcher.** The launcher is unsigned and frozen
-- every change to it costs each user a Gatekeeper dialog -- while this package
updates through PyPI. So the item is drawn by `gammonview-helper run` itself,
and every machine the installer ever set up gets it on its next update, with no
change to the login item that runs it.

**One process, two threads.** AppKit wants the main thread, so the work loop
(`daemon.serve`) runs on a background one and the two speak only through a
`daemon.Control`. The menu never touches the network on the main thread except
in Quit, where nothing else is left to keep responsive.

**Quit has to get past launchd.** The login item is `KeepAlive`, so a helper
that merely exits is restarted within a minute -- which, from the menu, reads
as Quit not working. Under the login item, Quit therefore boots the job out of
launchd: stopped until the next login, or until somebody opens "GammonView
Helper" in Applications (`macapp.py`), which this item writes for exactly that.
Pause is the control for "not right now"; Quit is the one for "not today", and
it asks first, saying so.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from importlib.resources import files
from typing import Callable

from . import __version__
from .config import Config, machine_name
from .daemon import Control, sign_off
from .store import load_token

#: The login item's launchd label: `LoginItem.swift` and
#: `deploy/com.gammonview.helper.plist` both write it.
LAUNCHD_LABEL = "com.gammonview.helper"

#: How often the menu redraws from `Control`. The progress the menu shows is
#: posted to the site once a second, so faster would only redraw the same
#: number.
REFRESH_INTERVAL = 1.0

#: How long Quit waits for the work loop to fail its running job. The loop
#: checks for a stop once per `PROGRESS_INTERVAL` (1s), so this is that plus
#: the request.
QUIT_GRACE = 5.0


def available() -> bool:
    """Whether `run` should draw the item.

    Not over SSH: a menu bar belongs to whoever is sitting at the machine, and a
    process started from a remote shell that tries to join that session either
    fails or puts an icon in front of somebody else.
    """
    if sys.platform != "darwin" or os.environ.get("SSH_CONNECTION"):
        return False
    try:
        import rumps  # noqa: F401
    except ImportError:
        return False
    return True


def _icon(paused: bool) -> str:
    name = "menubar-paused.png" if paused else "menubar.png"
    return str(files("gvhelper") / "resources" / name)


def under_login_item() -> bool:
    """True when launchd started this process as the login item.

    launchd puts the job's label in `XPC_SERVICE_NAME`; a terminal has `0` or
    nothing. Asked so that Quit from a helper somebody started by hand does
    not boot out a login item that is a different process.
    """
    return os.environ.get("XPC_SERVICE_NAME") == LAUNCHD_LABEL


def status_line(control: Control) -> str:
    """The menu's first line, in the words the website would use."""
    progress = control.progress
    percent = ""
    if progress is not None:
        done, total = progress.get()
        if total:
            percent = f" {100 * done // total}%"
    if control.paused:
        if progress is not None:
            return f"Pausing after this match…{percent}"
        return "Paused"
    return {
        "starting": "Starting…",
        "unlinked": "Not linked to an account",
        "idle": "Ready for matches",
        "busy": f"Analysing a match…{percent}",
        "retrying": "Can't reach GammonView, retrying",
    }.get(control.state, "Ready for matches")


class _MenuLink:
    """`cli.pair`'s narrator, for a menu: every event goes to the main thread.

    The pairing flow runs on its own thread (it blocks for up to a minute
    waiting for the browser), and AppKit may only be touched from the main
    one, so this puts events on a queue that the menu's timer drains.
    """

    def __init__(self, events: queue.Queue):
        self._events = events

    def offer(self, machine: str, platform: str, word: str, url: str, expires_in: int) -> None:
        self._events.put(("offer", word, url))

    def linked(self, store_name: str, worker_id: str | None) -> None:
        self._events.put(("linked",))

    def failed(self, reason: str, retry: bool) -> None:
        self._events.put(("failed", reason))


def run(cfg: Config, control: Control, loop: Callable[[], int]) -> int:
    """Run `loop` on a background thread and the menu on this one.

    Returns the loop's exit code if it ends by itself, and 0 on Quit.
    """
    import rumps
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

    from . import macapp
    from .cli import pair

    if under_login_item():
        # Refreshed on every start, so an update reaches it: the one way back
        # after Quit that does not need a terminal or a log-out.
        macapp.ensure(os.path.abspath(sys.argv[0]), __version__)

    # No Dock icon and no app menu: this is a menu-bar item, not an app
    # somebody switches to.
    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory
    )

    result: dict = {}

    def work() -> None:
        try:
            result["code"] = loop()
        except BaseException as e:  # noqa: BLE001 - reported by the menu, then exited
            print(f"helper loop failed: {type(e).__name__}: {e}", flush=True)
            result["code"] = 1

    worker = threading.Thread(target=work, name="helper-loop", daemon=True)

    events: queue.Queue = queue.Queue()

    def bring_forward() -> None:
        # An accessory app is never frontmost by itself, so an alert from one
        # opens *behind* whatever the user is looking at.
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    class HelperMenu(rumps.App):
        def __init__(self) -> None:
            super().__init__(
                "GammonView Helper", icon=_icon(False), template=True, quit_button=None
            )
            self.status = rumps.MenuItem("Starting…")
            self.toggle = rumps.MenuItem("Pause", callback=self.on_toggle)
            self.link = rumps.MenuItem("Link This Computer…", callback=self.on_link)
            self.quit = rumps.MenuItem("Quit GammonView Helper", callback=self.on_quit)
            self.menu = [self.status, None, self.toggle, self.link, None, self.quit]
            self._linking = False
            self._paused_drawn = False
            self._timer = rumps.Timer(self.refresh, REFRESH_INTERVAL)
            self._timer.start()

        # --- drawing ---------------------------------------------------------

        def refresh(self, _timer=None) -> None:
            if not worker.is_alive() and not control.stopping:
                # The loop ended on its own -- nothing does that today but an
                # exception -- and a menu over a dead loop would claim to be
                # ready while doing nothing.
                rumps.quit_application()
                return

            while True:
                try:
                    event = events.get_nowait()
                except queue.Empty:
                    break
                self.on_link_event(event)

            self.status.title = status_line(control)
            self.toggle.title = "Resume" if control.paused else "Pause"
            # Linking an already-linked machine registers it twice, and the
            # account then lists one computer as two. So the item is there only
            # when there is nothing linked yet.
            self.link.hidden = control.state != "unlinked"
            self.link.title = "Linking…" if self._linking else "Link This Computer…"
            # A percentage beside the icon while analysing, and nothing
            # otherwise: the icon alone should be the quiet state.
            progress = control.progress
            done, total = progress.get() if progress is not None else (0, 0)
            self.title = f"{100 * done // total}%" if total else None
            if control.paused != self._paused_drawn:
                self.icon = _icon(control.paused)
                self._paused_drawn = control.paused

        # --- Pause / Resume --------------------------------------------------

        def on_toggle(self, _sender) -> None:
            if control.paused:
                control.resume()
            else:
                control.pause()
                # Said now rather than when the loop next looks up: that can be
                # a 25-second poll or a whole match away, and the site would
                # spend it routing work here. The loop says it again once idle,
                # in case this one did not get through.
                threading.Thread(target=self._sign_off, daemon=True).start()
            self.refresh()

        def _sign_off(self) -> None:
            from .client import WorkerClient

            token = load_token(cfg)
            if not token:
                return
            client = WorkerClient(cfg, token)
            try:
                sign_off(client)
            finally:
                client.close()

        # --- Link ------------------------------------------------------------

        def on_link(self, _sender) -> None:
            if self._linking:
                return
            self._linking = True
            out = _MenuLink(events)
            threading.Thread(
                target=lambda: (pair(cfg, out, machine_name()), events.put(("done",))),
                name="helper-link",
                daemon=True,
            ).start()
            self.refresh()

        def on_link_event(self, event: tuple) -> None:
            kind = event[0]
            if kind == "offer":
                _, word, url = event
                bring_forward()
                rumps.alert(
                    title="Link this computer",
                    message=(
                        "Your browser is opening GammonView. Sign in if you are "
                        "asked, then choose this word:\n\n"
                        f"{word}\n\n"
                        "If the word is not one of the three you are shown, "
                        "close the page: something is wrong.\n\n"
                        f"If no browser opened, go to:\n{url}"
                    ),
                )
            elif kind == "linked":
                # Nothing to restart: the loop is waiting on the credential
                # store, and finds this within `LINK_POLL_INTERVAL`.
                bring_forward()
                rumps.alert(
                    title="This computer is linked",
                    message="Analyses you start on gammonview.com will now run here.",
                )
            elif kind == "failed":
                bring_forward()
                rumps.alert(title="Linking did not finish", message=event[1])
            elif kind == "done":
                self._linking = False

        # --- Quit ------------------------------------------------------------

        def on_quit(self, _sender) -> None:
            if under_login_item():
                bring_forward()
                confirmed = rumps.alert(
                    title="Quit GammonView Helper?",
                    message=(
                        "It will stop analysing matches until you next log in, "
                        "or until you open GammonView Helper in your "
                        "Applications folder.\n\n"
                        "To stop for a while, choose Pause instead."
                    ),
                    ok="Quit",
                    cancel="Cancel",
                )
                if confirmed != 1:
                    return
            control.stop()
            # The loop fails a running job itself (`daemon.QUIT_MESSAGE`) the next
            # time it checks, which is within a second. Waited for here, because
            # once launchd takes this process the job has no answer at all.
            worker.join(QUIT_GRACE)
            self._sign_off()
            if under_login_item():
                # This call ends the process: launchd sends SIGTERM and waits.
                # Anything after it runs only if the bootout failed, in which
                # case exiting is the next best thing -- and launchd restarting
                # us a minute later is at worst what Quit did before.
                subprocess.run(
                    ["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
                    check=False,
                )
            rumps.quit_application()

    menu = HelperMenu()
    worker.start()
    menu.run()
    return result.get("code", 0) if not control.stopping else 0

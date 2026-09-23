# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Where the helper keeps its settings, and what it decides for itself.

Two things live here that are easy to get wrong on someone else's computer.

**The install root is private.** `~/Library/Application Support/GammonView/`,
`%LOCALAPPDATA%\\GammonView\\`, `~/.config/gammonview/` -- never the user's
PATH, never their Python, never a dotfile in `$HOME` that a backup tool will
sweep up. The launcher installs into the same directory this writes to, so
uninstall stays "one directory and one login item".

**The machine is not ours to fill.** The analyser will happily take every core
on the box, and a background process that makes a laptop feel slow gets
uninstalled -- which is the failure mode this whole feature dies of. So the
defaults here are deliberately *under* what the hardware could do, and the
knobs to raise them are documented rather than hidden.
"""

from __future__ import annotations

import json
import os
import platform
import socket
from dataclasses import dataclass
from pathlib import Path

#: The site the helper talks to, and the base path of the accounts service on
#: it.
#:
#: This line pointed at beta until accounts went live on gammonview.com
#: (2026-09-21, step 6 of `docs/DesktopHelper.md`), because a helper shipped
#: before the relay existed on production would have paired against a 404. It
#: has to be production *before* the first PyPI or GitHub release and not
#: after: a stranger running `uv tool install` gets this default, beta is
#: tailnet-only, and the failure it produces is a connection error with nothing
#: in it to explain itself. A published version number cannot be taken back.
#:
#: Beta is now the opt-in it is everywhere else in GammonView --
#: `GAMMONVIEW_SITE`, or `site` in `config.json`.
DEFAULT_SITE = "https://gammonview.com"

#: The accounts service is proxied at `/accounts/` on the site origin, which is
#: also what the browser client's `DEFAULT_API_BASE` says. The relay is a router
#: on that service, not a service of its own, so every path here hangs off it.
DEFAULT_API_PATH = "/accounts"


def config_dir() -> Path:
    """The helper's private directory, created on demand.

    `GAMMONVIEW_CONFIG_DIR` overrides it, which is what the tests use and what
    lets someone run a second helper against beta and production on one machine
    without the two overwriting each other's config.
    """
    override = os.environ.get("GAMMONVIEW_CONFIG_DIR")
    if override:
        path = Path(override).expanduser()
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        path = Path(base) / "GammonView"
    elif platform.system() == "Darwin":
        path = Path.home() / "Library" / "Application Support" / "GammonView"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
        path = Path(base) / "gammonview"
    path.mkdir(parents=True, exist_ok=True)
    return path


def machine_name() -> str:
    """What the confirmation screen will show as the name of this computer.

    An unverified claim, and presented as one by the page that displays it --
    the relay stores whatever is sent. It is here to help a person recognise
    their own laptop, not to identify anything.

    The hostname rather than anything cleverer, with the mDNS `.local` suffix
    stripped because "MacBook-Pro.local" reads as a fault to a non-technical
    reader and "MacBook-Pro" reads as their computer.
    """
    name = socket.gethostname() or "Unknown computer"
    for suffix in (".local", ".lan", ".home", ".localdomain"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name.strip() or "Unknown computer"


def platform_name() -> str:
    """A short, human description of the OS, for the same screen.

    Deliberately coarse. It is read by somebody deciding whether the machine
    asking to be linked is the machine in front of them, and "macOS 15.6" helps
    with that where a full `platform.platform()` string does not.
    """
    system = platform.system()
    if system == "Darwin":
        release = platform.mac_ver()[0]
        return f"macOS {release}" if release else "macOS"
    if system == "Windows":
        release = platform.release()
        return f"Windows {release}" if release else "Windows"
    if system == "Linux":
        return "Linux"
    return system or "Unknown"


#: "Let `gvanalysis` size it." Zero is that value on both parallelism axes --
#: `jobs=0` picks the worker count from the core count, `threads=0` gives each
#: worker every core -- and it is the default for both. See `Config.jobs`.
AUTO = 0


@dataclass
class Config:
    """Everything the helper needs to know that is not a credential.

    The token is *not* here on purpose -- it lives in `store.py`, in the
    platform credential store. A config file is something users copy, paste into
    a support email and sync to a cloud drive; a token must not be along for any
    of that.
    """

    site: str = DEFAULT_SITE
    api_path: str = DEFAULT_API_PATH
    #: The relay's public id for this machine, learned at pairing. Kept so that
    #: `status` can say which row in account settings is this computer.
    worker_id: str | None = None
    #: Decision-level parallelism (`gvanalysis`'s `jobs`) and engine threads
    #: within each worker (`threads`). Both are `AUTO`, and the sizing is
    #: upstream's.
    #:
    #: **This used to be half the cores capped at six, times two threads, and
    #: the reasoning under it was wrong in a way that cost real wall clock.**
    #: It treated the two axes as substitutes and treated starving the analysis
    #: of cores as how a helper stays out of the way. gvanalysis 1.1.0 measured
    #: both axes on four machines: `checker_eval` elevates ~15 candidates per
    #: decision with a hardcoded `n_threads=1` and overlaps them, so **one
    #: decision draws about seven cores and no more**. Threads fill a decision;
    #: processes fill a machine. Half a 24-core box at two threads a worker
    #: therefore asked for ~12 cores' worth of work and left the rest idle.
    #:
    #: So the split is `gvanalysis`'s now -- `ceil(cpu / 7)` workers, floor of
    #: two, each with every core -- which also means it moves when upstream
    #: re-measures rather than when somebody remembers to edit this file. On 24
    #: cores that is 4x0 at 234.97s against serial's 338.26s.
    #:
    #: Politeness is `nice` below, which is the axis that actually buys it.
    #: Niceness is inherited across the spawn, so every engine worker yields to
    #: whatever the user is doing -- and it costs nothing on an idle machine,
    #: where holding cores back costs the same whether anyone is sitting there
    #: or not. Someone who wants a hard cap still writes `jobs` into
    #: `config.json`, and an explicit setting survives a save.
    jobs: int = AUTO
    threads: int = AUTO
    #: Scheduling niceness for the analysis process. Positive means "yield to
    #: everything the user is actually doing", which is the entire point; the
    #: macOS launchd template in `server/deploy/` carries the same idea as a
    #: `Nice` key and `docs/DesktopHelper.md` says it must survive.
    #:
    #: Ignored on Windows, which has no `os.nice` -- `runner.py` lowers the
    #: process priority there by a different call, or not at all.
    nice: int = 10

    @property
    def api_base(self) -> str:
        """Absolute base URL of the accounts service, with no trailing slash."""
        return f"{self.site.rstrip('/')}{self.api_path.rstrip('/')}"

    @property
    def relay_base(self) -> str:
        return f"{self.api_base}/relay"

    def link_url(self, code: str) -> str:
        """Where the browser is sent to confirm a pairing.

        A hash route, because the site is a hash-history SPA -- a path-style URL
        would be served the index and then navigate nowhere.
        """
        return f"{self.site.rstrip('/')}/#/link-helper?code={code}"


def config_path() -> Path:
    return config_dir() / "config.json"


def load() -> Config:
    """Read the config, falling back to defaults for anything missing.

    A corrupt file is treated as an absent one rather than as an error. The only
    thing in it that cannot be reconstructed is `worker_id`, which is cosmetic;
    refusing to start because a JSON file lost a brace would strand a user with
    no obvious way back.

    Environment variables win over the file, so a support session can point a
    helper at another site for one run without editing anything.
    """
    data: dict = {}
    try:
        data = json.loads(config_path().read_text())
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}

    # `api_path` is read with a sentinel rather than `or`, because the empty
    # string is a *meaningful* value here and a falsy one: a developer running
    # the accounts service directly has no nginx in front of it, so its routes
    # sit at the root and the correct path is "". An `or` chain would silently
    # rewrite that to "/accounts" and every request would 404.
    api_path = os.environ.get("GAMMONVIEW_API_PATH")
    if api_path is None:
        api_path = data.get("api_path")
    if api_path is None:
        api_path = DEFAULT_API_PATH

    cfg = Config(
        site=os.environ.get("GAMMONVIEW_SITE") or data.get("site") or DEFAULT_SITE,
        api_path=api_path,
        worker_id=data.get("worker_id"),
    )
    for name in ("jobs", "threads", "nice"):
        raw = os.environ.get(f"GAMMONVIEW_{name.upper()}", data.get(name))
        try:
            if raw is not None:
                setattr(cfg, name, int(raw))
        except (TypeError, ValueError):
            pass
    # Floored at `AUTO` rather than at 1, because zero is now a meaningful
    # setting on both axes and a negative one is nonsense that should land on
    # the default rather than on serial.
    cfg.jobs = max(AUTO, cfg.jobs)
    cfg.threads = max(AUTO, cfg.threads)
    return cfg


def save(cfg: Config) -> None:
    """Write the config back. Only the fields worth persisting.

    Not `jobs`/`threads`/`nice` unless they were already in the file. These are
    constants rather than computed values now, so the old reason -- freezing
    one machine's core count into a file that outlives it -- has gone; the
    remaining one is that writing `0` would be indistinguishable from somebody
    choosing it, and would pin this helper to today's sizing even after
    `gvanalysis` re-measures.
    """
    path = config_path()
    try:
        existing = json.loads(path.read_text())
        if not isinstance(existing, dict):
            existing = {}
    except (OSError, ValueError):
        existing = {}

    existing.update({"site": cfg.site, "api_path": cfg.api_path})
    if cfg.worker_id:
        existing["worker_id"] = cfg.worker_id
    path.write_text(json.dumps(existing, indent=2) + "\n")

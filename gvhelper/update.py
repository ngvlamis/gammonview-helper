# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Whether a newer helper is out, and installing it from the menu.

**What counts as released is what the site says**, not what PyPI has: the
installer installs the version pinned in `helper-release.json` on the site, and
a release reaches PyPI before that pin moves (`docs/DesktopHelper.md`, release
order). Asking PyPI would offer a version the site has not yet agreed to.

**Only an installer's install can update itself.** The installer keeps its own
`uv` beside the helper (`Paths.swift`), so the menu can run the very command
the installer's Update button runs, with the same environment, and then have
launchd restart the job onto the new code. A helper somebody installed with
`uv tool` or `pipx` belongs to their tool, and is told so rather than touched.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

import httpx

from . import USER_AGENT
from .config import Config

#: How often the menu asks. A release is a few times a year, and the answer is
#: a small static file from the site's own nginx.
CHECK_INTERVAL = 6 * 60 * 60


def latest(cfg: Config, client: httpx.Client | None = None) -> dict | None:
    """The site's release manifest, or None if it cannot be had right now."""
    url = f"{cfg.site.rstrip('/')}/helper-release.json"
    try:
        if client is not None:
            r = client.get(url)
        else:
            r = httpx.get(url, timeout=15, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        release = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(release, dict) or not isinstance(release.get("version"), str):
        return None
    return release


def _parts(version: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(p) for p in version.split("."))
    except ValueError:
        return None


def newer(release: str, current: str) -> bool:
    """True when `release` is a later plain X.Y.Z than `current`.

    Anything else -- a dev build, "unknown" from a checkout -- is never offered
    an update: somebody running one of those chose it.
    """
    a, b = _parts(release), _parts(current)
    return a is not None and b is not None and a > b


def installer_root(helper: str) -> Path | None:
    """The install root, when `helper` is the installer's entry point.

    The installer writes `<root>/bin/gammonview-helper` with its own `uv`
    beside it and the venv under `<root>/tools`; a `uv tool` install of
    somebody's own has neither in that arrangement.
    """
    bin_dir = Path(helper).parent
    root = bin_dir.parent
    if (bin_dir / "uv").is_file() and (root / "tools").is_dir():
        return root
    return None


def environment(root: Path) -> dict[str, str]:
    """The installer's `uvEnvironment`, which must stay the same as it."""
    return {
        **os.environ,
        "UV_TOOL_DIR": str(root / "tools"),
        "UV_TOOL_BIN_DIR": str(root / "bin"),
        "UV_CACHE_DIR": str(root / "cache"),
        "UV_PYTHON_INSTALL_DIR": str(root / "python"),
        "UV_PYTHON_PREFERENCE": "only-managed",
        "UV_NO_MODIFY_PATH": "1",
    }


def install(
    root: Path,
    release: dict,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str | None:
    """Install `release` the way the installer would. None, or what went wrong."""
    argv = [str(root / "bin" / "uv"), "tool", "install", "--force"]
    python = release.get("python")
    if isinstance(python, str) and python:
        argv += ["--python", python]
    argv.append(f"gammonview-helper=={release['version']}")
    done = run(argv, env=environment(root), capture_output=True, text=True)
    if done.returncode == 0:
        return None
    lines = [line for line in (done.stderr or "").splitlines() if line.strip()]
    return lines[-1] if lines else f"uv exited with status {done.returncode}"

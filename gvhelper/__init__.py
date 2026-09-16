# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The GammonView desktop helper: analyse your own matches on your own machine.

The helper is a **client**, never a server. It dials out to gammonview.com,
holds a long poll open waiting for work its owner queued from the browser, runs
`gvanalysis` locally, and posts the result back. Nothing listens on a port and
nothing needs a router touched -- see `docs/DesktopHelper.md` in the GammonView
repo, "The helper dials out; the VPS never reaches it", for why that is the
whole security story rather than a convenience.

Three modules carry the substance and the rest is plumbing:

  * `client.py`  -- the relay protocol, and the only place a URL is built.
  * `runner.py`  -- one job: bytes in, analysed gzipped `.gvab` out.
  * `daemon.py`  -- the loop that ties them together and reports progress.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def _resolve_version() -> str:
    """This distribution's version, or "unknown" when it is not installed.

    Mirrors `gvserver.__init__`'s resolver and for the same reason: the helper
    reports this to the relay, the site shows it in account settings, and a
    helper running from a checkout must still say *something* rather than raise
    at import.
    """
    try:
        return version("gammonview-helper")
    except PackageNotFoundError:  # pragma: no cover - a source checkout
        return "unknown"


__version__ = _resolve_version()

#: How the helper identifies itself in HTTP, and in the relay's `version` field.
USER_AGENT = f"gammonview-helper/{__version__}"

# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Fixtures for the helper suite.

Every test gets its own config directory, so nothing here can read or write the
developer's real helper -- which on this machine is a live pairing against a
running site.

`keyring` is disabled by default for the same reason, and a second time over:
a suite that stored tokens would leave real entries in the developer's login
keychain, and on CI it would either fail or silently write to a file backend.
The fallback path is what these tests exercise; `test_store.py` covers the
choice between the two.

Run: uv run pytest
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """A private config directory, and no keyring, for every test."""
    monkeypatch.setenv("GAMMONVIEW_CONFIG_DIR", str(tmp_path / "gv"))
    # Cleared so a developer's own environment cannot reach in and change what
    # the suite is asserting -- `load()` reads all three.
    for name in ("GAMMONVIEW_SITE", "GAMMONVIEW_API_PATH",
                 "GAMMONVIEW_JOBS", "GAMMONVIEW_THREADS", "GAMMONVIEW_NICE"):
        monkeypatch.delenv(name, raising=False)

    from gvhelper import store
    monkeypatch.setattr(store, "_keyring", lambda: None)
    yield
    importlib.reload(store)


@pytest.fixture
def cfg():
    from gvhelper.config import Config

    return Config(site="https://example.test")

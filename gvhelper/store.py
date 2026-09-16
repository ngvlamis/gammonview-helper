# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Where the worker token lives.

The token is a session for somebody's GammonView account, scoped so that it can
only reach the relay (`sessions.scope = 'worker'` -- see the accounts service's
`auth.py`). That scope is what bounds the damage if it leaks: it cannot read a
match, a note or an email address. It is still a credential, and it still gets
stored like one.

**The platform credential store first, a file second, and the difference is
stated out loud.** `keyring` reaches Keychain on macOS, the Credential Manager
on Windows and Secret Service on Linux. On a headless Linux box there is no
Secret Service, `keyring` raises, and the alternative is refusing to run -- so
there is a file fallback with 0600 on it. What there is *not* is a silent
fallback: `backend_name()` reports which one is in use and `status` prints it,
because "my token is in the Keychain" and "my token is in a file" are different
promises and the user is entitled to know which one they got.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .config import Config, config_dir

#: Service name in the platform credential store. One constant, because a
#: rename orphans every token already stored under the old one.
SERVICE = "gammonview-helper"

#: Filename of the fallback store. Beside the config, not in it: the config is
#: the file a user is asked to paste into a support thread.
FALLBACK_NAME = "worker-token"


def _keyring():
    """The keyring module, or None if it is absent or has no usable backend.

    Imported lazily and probed rather than trusted. `keyring` installs happily
    everywhere and then picks a `fail.Keyring` backend when the platform has no
    store, whose every call raises -- so the import succeeding proves nothing
    and the backend has to be asked.
    """
    try:
        import keyring
        from keyring.backends import fail
    except Exception:  # pragma: no cover - keyring absent
        return None
    try:
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
    except Exception:  # pragma: no cover - a backend that fails to load
        return None
    return keyring


def _account(cfg: Config) -> str:
    """The credential's account name: the API base it is good for.

    Keying on the base rather than on a fixed string is what lets one machine
    hold a beta token and a production token at once without either standing in
    for the other -- which matters most during step 6, when the same helper is
    pointed at production and must not silently present beta's token to it.
    """
    return cfg.api_base


def _fallback_path() -> Path:
    return config_dir() / FALLBACK_NAME


def backend_name(cfg: Config) -> str:
    """Which store is in use, in words fit to print."""
    kr = _keyring()
    if kr is None:
        return f"file ({_fallback_path()})"
    try:
        return f"{kr.get_keyring().name}"
    except Exception:  # pragma: no cover
        return "keyring"


def load_token(cfg: Config) -> str | None:
    kr = _keyring()
    if kr is not None:
        try:
            token = kr.get_password(SERVICE, _account(cfg))
            if token:
                return token
        except Exception:  # pragma: no cover - a locked or broken keychain
            pass
    try:
        text = _fallback_path().read_text().strip()
    except OSError:
        return None
    # The file holds one token per API base, so a helper pointed at beta does
    # not read production's line. `store.py` writes the same shape.
    for line in text.splitlines():
        base, _, token = line.partition(" ")
        if base == _account(cfg) and token:
            return token
    return None


def save_token(cfg: Config, token: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE, _account(cfg), token)
            return
        except Exception:  # pragma: no cover - a locked keychain
            pass
    _write_fallback(cfg, token)


def _write_fallback(cfg: Config, token: str | None) -> None:
    """Rewrite the fallback file with this base's line set or removed.

    Created 0600 *before* anything is written to it, not chmodded afterwards:
    between the open and the chmod there is a window in which the file is
    world-readable and holds the token, and on a shared machine that window is
    the whole vulnerability.
    """
    path = _fallback_path()
    lines = []
    try:
        for line in path.read_text().splitlines():
            base, _, _rest = line.partition(" ")
            if base != _account(cfg):
                lines.append(line)
    except OSError:
        pass
    if token:
        lines.append(f"{_account(cfg)} {token}")

    if not lines:
        try:
            path.unlink()
        except OSError:
            pass
        return

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")


def clear_token(cfg: Config) -> None:
    """Forget this machine's token.

    Both stores, unconditionally. A helper that fell back to the file once and
    reached the keychain later would otherwise leave a live credential behind in
    whichever one this call did not think to look at.
    """
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SERVICE, _account(cfg))
        except Exception:  # pragma: no cover - nothing stored there
            pass
    _write_fallback(cfg, None)

# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Where the worker token lives, and what happens when the good place is gone.

The autouse fixture disables `keyring`, so unless a test says otherwise these
exercise the file fallback -- which is the path that can get the permissions
wrong, and therefore the one worth testing hardest.
"""

from __future__ import annotations

import stat

from gvhelper import store
from gvhelper.config import Config


def test_a_token_round_trips(cfg):
    store.save_token(cfg, "tok-1")
    assert store.load_token(cfg) == "tok-1"


def test_nothing_stored_reads_as_nothing(cfg):
    assert store.load_token(cfg) is None


def test_the_fallback_file_is_not_readable_by_anyone_else(cfg):
    """0600, and created that way rather than chmodded afterwards: between an
    open and a later chmod there is a window in which the file is world-readable
    and already holds the token."""
    store.save_token(cfg, "tok-1")
    mode = store._fallback_path().stat().st_mode
    assert not mode & stat.S_IRGRP
    assert not mode & stat.S_IROTH
    assert mode & stat.S_IRUSR


def test_beta_and_production_tokens_do_not_stand_in_for_each_other():
    """Keyed on the API base, which is what makes step 6 safe: the same helper
    pointed at production must not present beta's token to it."""
    beta = Config(site="https://beta.example.test")
    prod = Config(site="https://example.test")
    store.save_token(beta, "beta-token")
    store.save_token(prod, "prod-token")
    assert store.load_token(beta) == "beta-token"
    assert store.load_token(prod) == "prod-token"


def test_clearing_one_site_leaves_the_other(cfg):
    other = Config(site="https://other.test")
    store.save_token(cfg, "a")
    store.save_token(other, "b")
    store.clear_token(cfg)
    assert store.load_token(cfg) is None
    assert store.load_token(other) == "b"


def test_clearing_the_last_token_removes_the_file(cfg):
    """An empty credentials file is a thing a user would reasonably read as
    "something is still stored here"."""
    store.save_token(cfg, "a")
    store.clear_token(cfg)
    assert not store._fallback_path().exists()


def test_the_backend_is_named_out_loud(cfg):
    """"My token is in the Keychain" and "my token is in a file" are different
    promises. The fallback is allowed; a *silent* fallback is not."""
    assert "file" in store.backend_name(cfg)


def test_a_keyring_that_works_is_preferred(cfg, monkeypatch):
    stored = {}

    class FakeKeyring:
        def get_password(self, service, account):
            return stored.get((service, account))

        def set_password(self, service, account, token):
            stored[(service, account)] = token

        def delete_password(self, service, account):
            stored.pop((service, account), None)

        def get_keyring(self):
            return type("B", (), {"name": "Fake Keychain"})()

    monkeypatch.setattr(store, "_keyring", lambda: FakeKeyring())
    store.save_token(cfg, "tok")
    assert stored[(store.SERVICE, cfg.api_base)] == "tok"
    assert store.load_token(cfg) == "tok"
    # and nothing was written to disk
    assert not store._fallback_path().exists()
    assert store.backend_name(cfg) == "Fake Keychain"


def test_a_keyring_that_raises_falls_back_rather_than_failing(cfg, monkeypatch):
    """A locked keychain must not stop the helper from being linked. The user
    would have no idea what to do with the exception, and the fallback is a
    downgrade rather than a failure."""

    class BrokenKeyring:
        def get_password(self, *a):
            raise RuntimeError("locked")

        def set_password(self, *a):
            raise RuntimeError("locked")

        def delete_password(self, *a):
            raise RuntimeError("locked")

    monkeypatch.setattr(store, "_keyring", lambda: BrokenKeyring())
    store.save_token(cfg, "tok")
    assert store.load_token(cfg) == "tok"


def test_clearing_sweeps_both_stores(cfg, monkeypatch):
    """A helper that fell back to a file once and reached the keychain later
    would otherwise leave a live credential in whichever store the clear did not
    think to look at."""
    store.save_token(cfg, "in-a-file")

    cleared = []

    class FakeKeyring:
        def get_password(self, *a):
            return None

        def set_password(self, *a):
            pass

        def delete_password(self, service, account):
            cleared.append(account)

    monkeypatch.setattr(store, "_keyring", lambda: FakeKeyring())
    store.clear_token(cfg)
    assert cleared == [cfg.api_base]
    assert not store._fallback_path().exists()

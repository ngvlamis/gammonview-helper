# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Config: where it lives, what it decides, and what it refuses to persist."""

from __future__ import annotations

import json

from gvhelper import config


def test_the_config_directory_is_private_to_this_test():
    """The autouse fixture points every test at its own directory, which is
    also the mechanism that keeps the suite off a developer's real helper."""
    path = config.config_dir()
    assert path.exists()
    assert path.name == "gv"


def test_defaults_point_at_beta():
    """Deliberately the opposite of every other default in GammonView.

    A helper shipped before the relay exists on production would pair against a
    404, so beta is the safe default until step 6 of the plan flips it."""
    assert config.Config().site == "https://beta.gammonview.com"


def test_the_relay_hangs_off_the_accounts_service():
    """The relay is a router on the accounts service, not a service of its own,
    so every URL the helper builds is a suffix of one base."""
    cfg = config.Config(site="https://example.test")
    assert cfg.api_base == "https://example.test/accounts"
    assert cfg.relay_base == "https://example.test/accounts/relay"


def test_a_trailing_slash_on_the_site_does_not_double_up():
    cfg = config.Config(site="https://example.test/")
    assert cfg.api_base == "https://example.test/accounts"


def test_the_link_url_is_a_hash_route():
    """The site is a hash-history SPA. A path-style URL would be served the
    index and then navigate nowhere, which reads to a user as a broken link."""
    cfg = config.Config(site="https://example.test")
    assert cfg.link_url("abc") == "https://example.test/#/link-helper?code=abc"


def test_the_engine_sizes_its_own_parallelism():
    """Neither axis is picked here any more. The helper used to compute half
    the cores times two threads, which measured badly on a big machine -- one
    decision draws about seven cores, so threads fill a decision and processes
    fill a box, and `gvanalysis` is where that measurement lives."""
    cfg = config.Config()
    assert cfg.jobs == config.AUTO
    assert cfg.threads == config.AUTO


def test_a_nonsense_parallelism_lands_on_auto_not_on_serial():
    """Zero is meaningful on both axes now, so the floor had to move off 1.
    A negative must not quietly become the slowest possible setting."""
    config.config_path().write_text(json.dumps({"jobs": -4, "threads": -1}))
    cfg = config.load()
    assert cfg.jobs == config.AUTO
    assert cfg.threads == config.AUTO


def test_the_helper_yields_to_the_user_by_default():
    """`nice` is now the *only* thing standing between an analysis and an
    unusable machine -- the old core-starving default is gone -- so this is
    load-bearing in a way it was not before."""
    assert config.Config().nice > 0


def test_a_corrupt_config_reads_as_an_absent_one():
    """Refusing to start because a JSON file lost a brace would strand someone
    with no obvious way back, and nothing in the file is unreconstructable."""
    config.config_path().write_text("{not json")
    cfg = config.load()
    assert cfg.site == config.DEFAULT_SITE


def test_the_environment_wins_over_the_file(monkeypatch):
    """So a support session can point a helper elsewhere for one run without
    editing anything."""
    config.config_path().write_text(json.dumps({"site": "https://file.test"}))
    monkeypatch.setenv("GAMMONVIEW_SITE", "https://env.test")
    assert config.load().site == "https://env.test"


def test_saving_does_not_write_the_parallelism_defaults():
    """Writing `0` would be indistinguishable from somebody choosing it, and
    would pin this helper to today's sizing after `gvanalysis` re-measures."""
    cfg = config.load()
    cfg.worker_id = "w1"
    config.save(cfg)
    written = json.loads(config.config_path().read_text())
    assert written["worker_id"] == "w1"
    assert "jobs" not in written
    assert "threads" not in written


def test_an_explicit_parallelism_setting_survives_a_save():
    """The flip side: a number the user chose is theirs, and a save that is
    about something else must not drop it."""
    config.config_path().write_text(json.dumps({"jobs": 3}))
    cfg = config.load()
    assert cfg.jobs == 3
    cfg.worker_id = "w1"
    config.save(cfg)
    assert json.loads(config.config_path().read_text())["jobs"] == 3


def test_the_machine_name_loses_its_mdns_suffix(monkeypatch):
    """"MacBook-Pro.local" reads as a fault to a non-technical reader, and this
    string is shown on the screen where somebody decides whether to trust it."""
    monkeypatch.setattr(config.socket, "gethostname", lambda: "MacBook-Pro.local")
    assert config.machine_name() == "MacBook-Pro"


def test_a_nameless_machine_still_has_something_to_show(monkeypatch):
    monkeypatch.setattr(config.socket, "gethostname", lambda: "")
    assert config.machine_name() == "Unknown computer"


def test_an_empty_api_path_is_a_value_and_not_a_missing_one(monkeypatch):
    """A developer running the accounts service directly has no nginx in front
    of it, so its routes sit at the root and "" is correct. An `or` chain would
    rewrite that to "/accounts" and every request would 404 -- which looks
    exactly like a service that is not running."""
    monkeypatch.setenv("GAMMONVIEW_API_PATH", "")
    monkeypatch.setenv("GAMMONVIEW_SITE", "http://127.0.0.1:8790")
    cfg = config.load()
    assert cfg.api_base == "http://127.0.0.1:8790"
    assert cfg.relay_base == "http://127.0.0.1:8790/relay"

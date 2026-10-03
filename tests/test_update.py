# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Whether the menu offers an update, and what installing it runs."""

from __future__ import annotations

import subprocess

import httpx
import respx

from gvhelper import update


@respx.mock
def test_the_release_is_whatever_the_site_pins(cfg):
    respx.get("https://example.test/helper-release.json").mock(
        return_value=httpx.Response(200, json={"version": "0.5.1", "python": "3.13"})
    )
    assert update.latest(cfg)["version"] == "0.5.1"


@respx.mock
def test_no_answer_is_no_update(cfg):
    respx.get("https://example.test/helper-release.json").mock(
        side_effect=httpx.ConnectError("offline")
    )
    assert update.latest(cfg) is None
    respx.get("https://example.test/helper-release.json").mock(
        return_value=httpx.Response(200, text="<html>")
    )
    assert update.latest(cfg) is None


def test_only_a_later_release_is_offered():
    assert update.newer("0.5.0", "0.4.0")
    assert update.newer("0.10.0", "0.9.3"), "compared as numbers, not text"
    assert not update.newer("0.4.0", "0.4.0")
    assert not update.newer("0.4.0", "0.5.0")
    assert not update.newer("0.5.0", "0.4.1.dev3+gabc"), "a dev build chose itself"
    assert not update.newer("0.5.0", "unknown")


def installer_layout(tmp_path):
    root = tmp_path / "GammonView"
    (root / "bin").mkdir(parents=True)
    (root / "tools").mkdir()
    (root / "bin" / "uv").write_text("")
    return root


def test_the_installers_install_is_recognised_by_its_uv(tmp_path):
    root = installer_layout(tmp_path)
    assert update.installer_root(str(root / "bin" / "gammonview-helper")) == root
    assert update.installer_root(str(tmp_path / ".local" / "bin" / "gammonview-helper")) is None


def test_installing_runs_what_the_installer_runs(tmp_path):
    root = installer_layout(tmp_path)
    seen = {}

    def run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw["env"]
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert update.install(root, {"version": "0.5.0", "python": "3.13"}, run) is None
    assert seen["argv"] == [
        str(root / "bin" / "uv"), "tool", "install", "--force",
        "--python", "3.13", "gammonview-helper==0.5.0",
    ]
    assert seen["env"]["UV_TOOL_DIR"] == str(root / "tools")
    assert seen["env"]["UV_PYTHON_PREFERENCE"] == "only-managed"


def test_a_failed_install_says_uvs_last_word(tmp_path):
    root = installer_layout(tmp_path)

    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "Resolving...\nerror: no network\n")

    assert update.install(root, {"version": "0.5.0"}, run) == "error: no network"

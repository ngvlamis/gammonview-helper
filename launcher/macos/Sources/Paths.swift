// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

import Foundation

/// Everything the installer creates, and nothing outside it.
///
/// One directory, plus one login item. That is the whole footprint, and it is
/// what makes the uninstall instruction a sentence rather than a page.
///
/// Note what is deliberately *not* here: `/usr/local`, `~/.local/bin`, the
/// user's PATH, their `uv`, their Python, their shell profile. Somebody who
/// installs this may already have a `uv` with their own tools in it and a
/// Python they care about, and an installer aimed at a non-technical audience
/// has no business touching either. Every `uv` invocation is given these paths
/// explicitly (`Installer.uvEnvironment`), so there is no ambient state that
/// could make the install behave differently on two machines.
enum Paths {
    /// Set `GAMMONVIEW_INSTALLER_ROOT` to install somewhere disposable.
    ///
    /// The only way to exercise this program without it taking over the
    /// machine it is being tested on, which is otherwise unavoidable: a real
    /// run seizes `com.gammonview.helper` from whatever was already serving it.
    /// So the variable does two things, and both are the same decision --
    /// it moves the install root, and `Paths.sandboxed` makes the login item
    /// step a no-op. A sandboxed install is a real install of everything
    /// except the part that is shared with the rest of the system.
    ///
    /// Not a supported interface and not mentioned to users; it is a test
    /// affordance in a program with no other way to be tested.
    static let sandboxed = ProcessInfo.processInfo.environment["GAMMONVIEW_INSTALLER_ROOT"]

    static let root: URL = {
        if let override = sandboxed, !override.isEmpty {
            return URL(fileURLWithPath: (override as NSString).expandingTildeInPath,
                       isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/GammonView", isDirectory: true)
    }()

    /// `uv` itself, and the `gammonview-helper` entry point it writes.
    static let bin = root.appendingPathComponent("bin", isDirectory: true)
    static let uv = bin.appendingPathComponent("uv")
    static let helper = bin.appendingPathComponent("gammonview-helper")

    /// `UV_TOOL_DIR`: the venv holding the helper and the engine.
    static let tools = root.appendingPathComponent("tools", isDirectory: true)
    /// `UV_PYTHON_INSTALL_DIR`: the interpreter uv downloads for us.
    static let python = root.appendingPathComponent("python", isDirectory: true)
    /// `UV_CACHE_DIR`: kept inside the root so that deleting the root really
    /// does reclaim the ~80 MB, rather than leaving it in `~/.cache/uv`.
    static let cache = root.appendingPathComponent("cache", isDirectory: true)

    static let logs = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/GammonView", isDirectory: true)
    static let launchAgents = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/LaunchAgents", isDirectory: true)

    static func create() throws {
        for dir in [root, bin, tools, python, cache, logs, launchAgents] {
            try FileManager.default.createDirectory(
                at: dir, withIntermediateDirectories: true)
        }
    }
}

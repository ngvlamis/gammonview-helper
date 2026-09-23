// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

import Foundation

/// Registering the helper so it is there after a reboot.
///
/// The plist is built here rather than copied from a resource because it has
/// to name an absolute path that is only known at run time, and because a
/// template file in the bundle is one more thing that can be edited into a
/// state the code does not expect.
///
/// It mirrors `deploy/com.gammonview.helper.plist`, whose comments carry the
/// reasoning in full. Three of its decisions matter enough to repeat:
///
/// * **No `Nice` key.** POSIX `nice(2)` is an increment, not a level, and the
///   helper already nices itself from `config.json`. launchd niceing it to 10
///   and the helper adding its own would land it at 20, the floor of the scale.
/// * **No `ProcessType`.** `Background` forces the job onto E-cores with
///   throttled I/O, measured ~3.5x slower, which would quietly undo the
///   parallelism the helper asks `gvanalysis` to size for it.
/// * **`GAMMONVIEW_SITE`, but only ever the manifest's answer.** This used to
///   be omitted, on the grounds that which site the helper talks to is the
///   *package's* business and baking an answer into a frozen installer is the
///   mistake this design exists to avoid. The first half of that was wrong.
///   The package's answer comes from `config.json` in the install root, and
///   the install root is a directory an earlier hand-install may already have
///   written -- so "the package decides" can mean "a file from months ago
///   decides", and the daemon polls beta forever while the person waits for
///   their machine to appear on gammonview.com. The second half still holds,
///   which is why the value is `manifest.site` and never a literal: it is
///   carried from the file we just fetched, not decided here, and a manifest
///   that names no site writes no key.
enum LoginItem {
    static let label = "com.gammonview.helper"

    static var plistURL: URL {
        Paths.launchAgents.appendingPathComponent("\(label).plist")
    }

    /// Internal so the generated plist can be linted without installing one.
    static func document(helper: URL, log: URL, site: String?) -> String {
        // Env beats `config.json` in `Config.load()`, which is the whole point
        // of putting it here: it is the one place that can outrank a stale file
        // the installer did not write and cannot safely delete.
        let environment = (site.map { value in
            """

                <key>EnvironmentVariables</key>
                <dict>
                    <key>GAMMONVIEW_SITE</key>
                    <string>\(value)</string>
                </dict>
            """
        } ?? "")
        return """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
        "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <!-- Written by the GammonView installer. Remove it with:
               launchctl bootout gui/$(id -u)/\(label)
               rm ~/Library/LaunchAgents/\(label).plist -->
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>\(label)</string>
            <key>ProgramArguments</key>
            <array>
                <string>\(helper.path)</string>
                <string>run</string>
            </array>
        \(environment)
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <true/>
            <key>ThrottleInterval</key>
            <integer>60</integer>
            <key>StandardOutPath</key>
            <string>\(log.path)</string>
            <key>StandardErrorPath</key>
            <string>\(log.path)</string>
        </dict>
        </plist>
        """
    }

    /// Install and start, replacing whatever was there.
    ///
    /// Idempotent, and it has to be: this runs on every upgrade as well as on
    /// the first install. The `bootout` is what stops a second run leaving two
    /// pollers sharing one worker id, which the relay sees as one machine
    /// contradicting itself about which job it holds.
    static func install(site: String?) throws {
        if Paths.sandboxed != nil {
            // A sandboxed install must not take `com.gammonview.helper` away
            // from whatever is already serving it on this machine.
            return
        }
        let log = Paths.logs.appendingPathComponent("helper.log")
        try document(helper: Paths.helper, log: log, site: site)
            .write(to: plistURL, atomically: true, encoding: .utf8)

        let uid = getuid()
        let launchctl = URL(fileURLWithPath: "/bin/launchctl")
        // Not loaded is the first install, not a failure -- launchd's own
        // phrasing for it is `Boot-out failed: 3: No such process`.
        _ = try? runProcess(launchctl, ["bootout", "gui/\(uid)/\(label)"], what: "launchctl bootout")
        try runProcess(
            launchctl, ["bootstrap", "gui/\(uid)", plistURL.path],
            what: "Starting GammonView Helper")
    }
}

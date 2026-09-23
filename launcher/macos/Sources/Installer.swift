// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

import Foundation

/// Everything the window is asked to show.
///
/// The installer never touches AppKit; it posts these, and the window renders
/// them. That is not tidiness -- the sequence below blocks for minutes at a
/// time, and anything it did to a view would be done off the main thread.
enum InstallerEvent {
    case step(title: String, detail: String)
    case notice(String)
    case word(String, URL)
    case finished(String)
    case failed(message: String, retry: Bool, downloadPage: Bool)
}

/// The install, as a sequence of steps that genuinely depend on each other.
///
/// The shape of this file is the plan's rule made literal: it finds `uv`,
/// installs the package, registers the login item, hands pairing to the
/// package, and stops. There is no protocol knowledge in here beyond the two
/// JSON shapes the helper promises, and no policy at all -- which version to
/// install is the manifest's answer, which site to talk to is the package's,
/// and what the confirmation word is is the relay's.
enum Installer {

    /// Run on a background queue; `emit` is called on the main queue.
    static func run(emit rawEmit: @escaping (InstallerEvent) -> Void) {
        let emit: (InstallerEvent) -> Void = { event in
            DispatchQueue.main.async { rawEmit(event) }
        }
        do {
            emit(.step(title: "Getting ready",
                       detail: "Checking which version to install\u{2026}"))
            let manifest = try Manifest.fetch()
            if let notice = manifest.notice, !notice.isEmpty { emit(.notice(notice)) }

            emit(.step(title: "Getting ready", detail: "Making room\u{2026}"))
            try Paths.create()
            try placeUv()

            emit(.step(
                title: "Installing",
                detail: "Downloading Python and the analysis engine. "
                    + "This is about 80 MB and can take a few minutes."))
            try installHelper(manifest) { line in
                emit(.step(title: "Installing", detail: line))
            }

            emit(.step(title: "Almost there", detail: "Starting GammonView Helper\u{2026}"))
            try LoginItem.install(site: manifest.site)

            if try isLinked(manifest) {
                // The upgrade path, which is most runs after the first. Pairing
                // again here would register a second worker against the account
                // and show the same computer twice in settings.
                emit(.finished("""
                    GammonView Helper is up to date and linked to your account.

                    It runs in the background. There is nothing else to do.
                    """))
                return
            }
            try pair(manifest, emit: emit)
        } catch let error as ManifestError {
            if case .launcherTooOld = error {
                emit(.failed(
                    message: error.localizedDescription, retry: false, downloadPage: true))
            } else {
                emit(.failed(
                    message: error.localizedDescription, retry: true, downloadPage: false))
            }
        } catch {
            emit(.failed(message: error.localizedDescription, retry: true, downloadPage: false))
        }
    }

    // MARK: - uv

    /// The `uv` that came in the bundle, copied into the install root.
    ///
    /// Copied rather than run from the bundle so that the app itself is
    /// disposable: somebody who drags the installer to the Trash after using it
    /// -- which is the normal thing to do with an installer -- must not take
    /// the working install with them.
    ///
    /// Copied on every run rather than only when missing, because it is a local
    /// copy of 35 MB and getting the *stale* case wrong is much worse than the
    /// milliseconds: a newer installer carrying a newer uv would otherwise keep
    /// using whatever the first one left behind, forever.
    private static func placeUv() throws {
        guard let bundled = Bundle.main.url(forResource: "uv", withExtension: nil) else {
            throw ShellError.couldNotStart(
                "uv",
                NSError(domain: "GammonView", code: 1, userInfo: [
                    NSLocalizedDescriptionKey:
                        "This copy of the installer is incomplete. Download it again."
                ]))
        }
        if FileManager.default.fileExists(atPath: Paths.uv.path) {
            try FileManager.default.removeItem(at: Paths.uv)
        }
        try FileManager.default.copyItem(at: bundled, to: Paths.uv)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755], ofItemAtPath: Paths.uv.path)
    }

    /// Where `uv` is told to put everything.
    ///
    /// Every one of these is set explicitly, and the reason is reproducibility
    /// rather than tidiness: a user who already has `uv` has `UV_CACHE_DIR` or
    /// a `uv.toml` that would otherwise decide where an *installer* writes.
    /// `UV_NO_MODIFY_PATH` is the one that matters most to somebody else's
    /// machine -- nothing here belongs on their PATH.
    private static var uvEnvironment: [String: String] {
        [
            "UV_TOOL_DIR": Paths.tools.path,
            "UV_TOOL_BIN_DIR": Paths.bin.path,
            "UV_CACHE_DIR": Paths.cache.path,
            "UV_PYTHON_INSTALL_DIR": Paths.python.path,
            // Its own interpreter, always. A system Python that later moves or
            // is upgraded out from under the venv is a helper that stops
            // starting, months after anybody was watching.
            "UV_PYTHON_PREFERENCE": "only-managed",
            "UV_NO_MODIFY_PATH": "1",
        ]
    }

    private static func installHelper(
        _ manifest: Manifest, onLine: @escaping (String) -> Void
    ) throws {
        var arguments = ["tool", "install", "--force"]
        if let python = manifest.python, !python.isEmpty {
            arguments += ["--python", python]
        }
        arguments.append("gammonview-helper==\(manifest.version)")
        try runProcess(
            Paths.uv,
            arguments,
            what: "Installing GammonView Helper",
            environment: uvEnvironment,
            // uv reports progress on stderr, one redraw at a time.
            onError: { line in
                let text = plain(line)
                if !text.isEmpty { onLine(text) }
            })
    }

    // MARK: - the helper's own answers

    /// Ask the installed helper whether this machine is already linked.
    ///
    /// `status` exits 1 when it is not linked, and that is an answer rather
    /// than a failure -- which is the point of the JSON being on stdout
    /// independently of the exit code. So the throw is caught and the parsed
    /// line is used either way.
    ///
    /// **The field to read is `link`, not `linked`.** They are two different
    /// questions and only one of them is ours. `linked` is `bool(token)` --
    /// whether there is a credential in the Keychain -- and a credential is
    /// exactly what survives every interesting way this breaks: a machine
    /// unlinked in account settings, an account deleted, a token restored onto
    /// a different computer by Migration Assistant. All three leave `linked`
    /// true and the site knowing nothing about the machine. `link` is the
    /// result of a real request (`status` calls `hello`), so `"working"` is
    /// the only value that means what the finished screen is about to claim.
    ///
    /// Getting this wrong was unrecoverable rather than merely wrong: the
    /// installer is the only thing a user of it has, and it told them they
    /// were done every single time they ran it again.
    ///
    /// `"unreachable"` therefore counts as not linked and we go on to pair.
    /// That cannot register the duplicate worker this check exists to prevent
    /// -- pairing talks to the same relay `hello` just failed to reach, so it
    /// fails too, with a message that says so and invites a retry.
    /// The helper's global options, which come *before* the subcommand.
    ///
    /// Only `--site`, and only when the manifest named one. Left off, the
    /// package answers for itself, which is the behaviour every launcher had
    /// before the manifest learned the field.
    ///
    /// Internal rather than private for the same reason `linkedVerdict` is:
    /// it is a decision, and a decision that can be checked without installing
    /// 80 MB is a decision that gets checked.
    static func globalArguments(_ manifest: Manifest) -> [String] {
        guard let site = manifest.site, !site.isEmpty else { return [] }
        return ["--site", site]
    }

    private static func isLinked(_ manifest: Manifest) throws -> Bool {
        var line: String?
        do {
            try runProcess(
                Paths.helper, globalArguments(manifest) + ["status", "--porcelain"],
                what: "Checking this computer",
                onOutput: { line = $0 })
        } catch { /* exit 1 means "not linked", and said so on stdout */ }

        return linkedVerdict(line)
    }

    /// The decision `isLinked` makes, separated from the process that feeds it
    /// so that it can be exercised without installing anything.
    static func linkedVerdict(_ line: String?) -> Bool {
        guard let line,
              let data = line.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            // Treated as not linked rather than as an error: the cost of being
            // wrong is one extra pairing, and the cost of erroring out is an
            // install that cannot finish.
            return false
        }
        if let link = object["link"] as? String {
            return link == "working"
        }
        // No `link` at all: a helper older than the one that added it, or a
        // future one that stopped emitting it. Fall back to the weaker field
        // rather than looping a working install through pairing every run.
        return object["linked"] as? Bool ?? false
    }

    /// Hand the device flow to the package and render what it says.
    ///
    /// `--no-browser` and then opening the URL ourselves, so the word is on
    /// screen before Safari takes the focus: a browser that arrives first is a
    /// browser the user reads first, and the one thing they must do is compare
    /// three words there against the one word here.
    private static func pair(
        _ manifest: Manifest, emit: @escaping (InstallerEvent) -> Void
    ) throws {
        var terminal: InstallerEvent?
        do {
            try runProcess(
                Paths.helper,
                globalArguments(manifest) + ["link", "--porcelain", "--no-browser"],
                what: "Linking this computer",
                onOutput: { line in
                    guard let data = line.data(using: .utf8),
                          let event = try? JSONSerialization.jsonObject(with: data)
                            as? [String: Any]
                    else { return }
                    switch event["event"] as? String {
                    case "offer":
                        if let word = event["word"] as? String,
                           let raw = event["url"] as? String,
                           let url = URL(string: raw) {
                            emit(.word(word, url))
                        }
                    case "linked":
                        terminal = .finished("""
                            This computer is linked.

                            GammonView Helper runs in the background from now on. \
                            Choose it on gammonview.com when you analyse a match.
                            """)
                    case "failed":
                        let reason = event["reason"] as? String ?? "Linking did not finish."
                        let retry = event["retry"] as? Bool ?? true
                        terminal = .failed(message: reason, retry: retry, downloadPage: false)
                    default:
                        break  // a field we do not know yet is not an error
                    }
                })
        } catch { /* `link` exits 1 after saying why, in an event we already have */ }

        emit(terminal ?? .failed(
            message: "Linking stopped unexpectedly.",
            retry: true, downloadPage: false))
    }
}

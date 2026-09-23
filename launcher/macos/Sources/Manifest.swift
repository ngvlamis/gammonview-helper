// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

import Foundation

/// What generation of launcher this is.
///
/// Compared against the manifest's `launcher_min`, and it is the only escape
/// hatch from the rule this whole design rests on -- *nothing that might need
/// to change may live in the launcher*. A launcher is downloaded once, is
/// never upgraded in place, and on macOS was accepted past Gatekeeper by a
/// person who will not do that twice. If the protocol between this program and
/// the helper ever has to break, this is how already-installed copies find out
/// rather than failing in a way nobody can diagnose.
///
/// It is worthless unless every launcher reads it from the first release,
/// which is why it exists at generation 1 with nothing yet to reject.
let launcherGeneration = 1

enum ManifestError: LocalizedError {
    case unreachable(Error)
    case malformed
    case launcherTooOld(Int)

    var errorDescription: String? {
        switch self {
        case .unreachable:
            return """
                Could not reach gammonview.com.

                Check that this computer is online, then try again.
                """
        case .malformed:
            return "gammonview.com sent something this installer could not read."
        case .launcherTooOld:
            return """
                This installer is out of date and cannot install the current helper.

                Download the current one from gammonview.com.
                """
        }
    }
}

/// Which version of the helper to install, as the site currently answers it.
///
/// Asked every run rather than compiled in, which is what makes a bad helper
/// release recoverable: the fix is three lines in a file on the web server,
/// and every launcher picks it up the next time somebody runs one. See
/// `public/helper-release.json` and its test in the GammonView repo.
struct Manifest {
    let version: String
    /// Which Python to install on, or `nil` for whatever `uv` prefers.
    ///
    /// Here rather than in the launcher because it is policy, and policy that
    /// moves. Given nothing, `uv` installs the newest managed Python it can
    /// find; the day a Python release lands that `bgsage` has no wheel for,
    /// every new install starts failing at once, and with the answer compiled
    /// into a frozen binary the only fix would be a new installer that
    /// everybody has to accept past Gatekeeper again. Here it is three
    /// characters on a web server.
    let python: String?
    let launcherMin: Int
    let notice: String?

    /// A constant in a real install, and deliberately not configurable there.
    ///
    /// This file decides which code gets installed on somebody's computer, so
    /// an environment variable that could repoint it would be the softest part
    /// of the whole install. The override is therefore honoured **only when
    /// `GAMMONVIEW_INSTALLER_ROOT` is also set** -- that is, only in a run that
    /// has already been told to install somewhere disposable and not to touch
    /// the login item. A shipped install has no path to it at all.
    static var url: URL {
        if Paths.sandboxed != nil,
           let override = ProcessInfo.processInfo.environment["GAMMONVIEW_INSTALLER_MANIFEST"],
           let parsed = URL(string: override) {
            return parsed
        }
        return URL(string: "https://gammonview.com/helper-release.json")!
    }

    static func fetch() throws -> Manifest {
        var request = URLRequest(url: url)
        // A rollback that a cache can defer is not a rollback. This file is
        // small, read once per run, and its whole value is being current.
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        request.timeoutInterval = 20

        var payload: Data?
        var failure: Error?
        let done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { data, response, error in
            if let error { failure = error } else if
                let http = response as? HTTPURLResponse, http.statusCode != 200 {
                failure = NSError(
                    domain: "GammonView", code: http.statusCode,
                    userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode)"])
            } else {
                payload = data
            }
            done.signal()
        }.resume()
        done.wait()

        if let failure { throw ManifestError.unreachable(failure) }
        guard let payload,
              let object = try? JSONSerialization.jsonObject(with: payload) as? [String: Any],
              let version = object["version"] as? String,
              let launcherMin = object["launcher_min"] as? Int
        else { throw ManifestError.malformed }

        if launcherMin > launcherGeneration { throw ManifestError.launcherTooOld(launcherMin) }

        // Unknown fields are ignored rather than rejected: the manifest is
        // allowed to grow, and a launcher frozen today must keep working
        // against one that has learned to say more.
        return Manifest(
            version: version,
            python: object["python"] as? String,
            launcherMin: launcherMin,
            notice: object["notice"] as? String)
    }
}

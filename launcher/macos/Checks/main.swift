// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

//
// The launcher's decisions, checked without installing anything.
//
// This is not a test suite for the installer -- most of what it does is
// irreducibly about seizing a machine, and `GAMMONVIEW_INSTALLER_ROOT` is the
// affordance for exercising that by hand. What is here instead is every place
// the launcher *decides* something from data it did not produce: two JSON
// shapes the helper emits, and one plist it wrote itself.
//
// Those are worth pinning precisely because they are cheap to get wrong and
// expensive to be wrong about. The installer ships frozen and unsigned, so a
// misreading is not a bug that gets fixed next week -- it is a bug sitting on
// other people's computers. Both readers below exist in their current form
// because an earlier one was wrong in exactly that way: `linkedVerdict` read
// `linked` (is there a credential) instead of `link` (does the relay agree),
// and told every user they were already set up, every time they ran it.
//
// Compiled against `../Sources/*.swift` directly rather than a copy. A check
// that drifts from the thing it checks is worse than no check.
//
// Run: launcher/macos/checks.sh
//

import Foundation

var failures = 0
func check(_ what: String, _ ok: Bool) {
    print(ok ? "  ok   \(what)" : "  FAIL \(what)")
    if !ok { failures += 1 }
}

// --- linkedVerdict: may the finished screen claim this machine is set up? ---
//
// False is always the safe answer: its cost is one extra pairing, which the
// relay makes idempotent anyway. True is the unrecoverable one.

print("linkedVerdict -- is this machine already linked?")
check("a working link is a link",
      Installer.linkedVerdict(#"{"event":"status","linked":true,"link":"working"}"#))
check("a revoked session is not",
      !Installer.linkedVerdict(#"{"linked":true,"link":"revoked"}"#))
check("an unreachable relay is not -- pairing will fail too, and say so",
      !Installer.linkedVerdict(#"{"linked":true,"link":"unreachable"}"#))
check("a credential with no relay answer is not",
      !Installer.linkedVerdict(#"{"linked":true,"link":null}"#))
check("no credential is not",
      !Installer.linkedVerdict(#"{"linked":false,"link":null}"#))
check("a helper too old to report `link` falls back to `linked`",
      Installer.linkedVerdict(#"{"event":"status","linked":true}"#))
check("...and that fallback still respects a false",
      !Installer.linkedVerdict(#"{"event":"status","linked":false}"#))
check("an unknown link value is not a link",
      !Installer.linkedVerdict(#"{"linked":true,"link":"something-new"}"#))
check("garbage is not a link", !Installer.linkedVerdict("not json"))
check("silence is not a link", !Installer.linkedVerdict(nil))
check("an empty line is not a link", !Installer.linkedVerdict(""))
check("a JSON array neither crashes nor links", !Installer.linkedVerdict("[1,2,3]"))

// --- globalArguments: which site a fresh install is pointed at ---

print("globalArguments -- the site comes from the manifest, never a literal")
func manifest(_ site: String?) -> Manifest {
    Manifest(version: "0.3.0", python: "3.13", site: site, launcherMin: 1, notice: nil)
}
check("a named site is passed through",
      Installer.globalArguments(manifest("https://gammonview.com"))
        == ["--site", "https://gammonview.com"])
check("no site means no flag, and the package answers for itself",
      Installer.globalArguments(manifest(nil)).isEmpty)
check("an empty site is not a site",
      Installer.globalArguments(manifest("")).isEmpty)

print("...and the flag lands before the subcommand, where argparse wants it")
let args = Installer.globalArguments(manifest("https://beta.gammonview.com"))
check("status",
      args + ["status", "--porcelain"]
        == ["--site", "https://beta.gammonview.com", "status", "--porcelain"])
check("link",
      args + ["link", "--porcelain", "--no-browser"]
        == ["--site", "https://beta.gammonview.com", "link", "--porcelain", "--no-browser"])

// --- accountVerdict: may the uninstaller say the account row is gone? ---
//
// Same asymmetry as `linkedVerdict`, pointing the other way: `.skipped` is the
// safe answer because it sends the user to check for themselves.

print("accountVerdict -- what an uninstall is allowed to claim")
func verdict(_ s: String?) -> String {
    switch Installer.accountVerdict(s) {
    case .removed: return "removed"
    case .left(let why): return "left:\(why)"
    case .skipped: return "skipped"
    }
}
check("removed is reported",
      verdict(#"{"event":"unlinked","account":"removed","reason":null}"#) == "removed")
check("left carries the reason",
      verdict(#"{"event":"unlinked","account":"left","reason":"no network"}"#)
        == "left:no network")
check("left with no reason still has a sentence",
      verdict(#"{"event":"unlinked","account":"left"}"#)
        == "left:could not reach gammonview.com")
check("skipped is skipped",
      verdict(#"{"event":"unlinked","account":"skipped"}"#) == "skipped")
check("a helper too old to report the field does not claim removal",
      verdict(#"{"event":"unlinked"}"#) == "skipped")
check("a value we do not know does not claim removal",
      verdict(#"{"account":"something-new"}"#) == "skipped")
check("garbage does not claim removal", verdict("not json at all") == "skipped")
check("silence does not claim removal", verdict(nil) == "skipped")
check("an empty line does not claim removal", verdict("") == "skipped")
check("a JSON array neither crashes nor claims removal", verdict("[1,2,3]") == "skipped")

// --- installedSite: which account an uninstall unlinks from ---
//
// Round-tripped through the document `install` actually writes, rather than a
// plist typed out here: the point of the check is that the writer and the
// reader agree, and two hand-written fixtures could agree with each other
// while both disagreeing with the installer.

print("installedSite -- which account an uninstall unlinks from")
let dir = URL(fileURLWithPath: NSTemporaryDirectory())
    .appendingPathComponent("gv-checks-\(UUID().uuidString)")
try! FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
defer { try? FileManager.default.removeItem(at: dir) }

let helper = URL(fileURLWithPath: "/tmp/gammonview-helper")
let log = URL(fileURLWithPath: "/tmp/helper.log")

func plist(_ name: String, site: String?) -> URL {
    let url = dir.appendingPathComponent(name)
    try! LoginItem.document(helper: helper, log: log, site: site)
        .write(to: url, atomically: true, encoding: .utf8)
    return url
}

let withSite = plist("with.plist", site: "https://beta.gammonview.com")
check("reads back exactly what install wrote",
      LoginItem.installedSite(at: withSite) == "https://beta.gammonview.com")

let noSite = plist("without.plist", site: nil)
check("no site written means no site read", LoginItem.installedSite(at: noSite) == nil)
check("an empty site is not a site",
      LoginItem.installedSite(at: plist("empty.plist", site: "")) == nil)
check("a missing plist is not an error",
      LoginItem.installedSite(at: dir.appendingPathComponent("nope.plist")) == nil)

let junk = dir.appendingPathComponent("junk.plist")
try! "not a plist at all".write(to: junk, atomically: true, encoding: .utf8)
check("an unparseable plist is not an error", LoginItem.installedSite(at: junk) == nil)

// launchd reads these, and a plist it cannot parse is a helper that never
// starts -- with nothing on screen to say so, because the installer has
// already finished by then.
print("both documents are plists launchd could actually load")
for url in [withSite, noSite] {
    let task = Process()
    task.executableURL = URL(fileURLWithPath: "/usr/bin/plutil")
    task.arguments = ["-lint", url.path]
    task.standardOutput = FileHandle.nullDevice
    try! task.run()
    task.waitUntilExit()
    check("plutil -lint \(url.lastPathComponent)", task.terminationStatus == 0)
}

print(failures == 0 ? "\nall checks passed" : "\n\(failures) FAILED")
exit(failures == 0 ? 0 : 1)

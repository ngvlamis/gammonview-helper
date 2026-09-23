# The launcher

The thing a person downloads. It finds `uv`, installs `gammonview-helper` from
PyPI into a private directory, registers a login item, and hands pairing to the
package it just installed. Then it stops.

That list is the whole specification, and its shortness is the design. The
launcher ships **unsigned**, so somebody gets past Gatekeeper exactly once —
which means anything that might change has to live on the other side of PyPI,
where updating it costs nobody a dialog. See `docs/DesktopHelper.md` in the
GammonView repository, *"The native surface is a launcher, and it is frozen"*.

    launcher/macos/    Swift + AppKit, universal binary, no Xcode project
    launcher/windows/  not built yet

## Building

```sh
launcher/macos/build.sh           # -> launcher/macos/build/GammonView Installer.app
VERSION=1.0.1 launcher/macos/build.sh
```

Needs the Command Line Tools and `curl`. No project file, no package manager,
no dependency to resolve: this artefact is meant to be rebuilt rarely and
identically, sometimes years apart, and every one of those is a thing that rots
in between.

**A full Xcode is needed for a release build**, and the script says so rather
than quietly producing something narrower. Swift needs back-deployment shim
libraries to target an OS older than the runtime features it links against, and
the Command Line Tools ship those for arm64 only — Apple stopped building the
x86_64 slices. With only the CLT the script still builds, with the Intel floor
raised from macOS 11 to macOS 13, behind a banner saying that is not the
shipping configuration. `sudo xcodebuild -license accept` is usually all that
is missing.

## What it is allowed to know

Three things, and each of them is fetched rather than compiled in:

| question | answered by |
| --- | --- |
| which helper version to install | `helper-release.json` on gammonview.com |
| which Python to install it on | the same file's `python` field |
| which site the helper talks to | the helper package's own default |

`launcher_min` in that file is the escape hatch, and the only one. A launcher
whose generation is below it stops and sends the user to the download page,
which is how an already-installed copy can be told the protocol moved. It is
worthless unless read from the first release, so it exists at generation 1 with
nothing yet to reject.

## What it installs, and where

Everything under `~/Library/Application Support/GammonView/` — `uv`, a private
CPython, the venv, the cache — plus one login item. Never the user's PATH,
never their `uv`, never their Python. Uninstall:

```sh
launchctl bootout gui/$(id -u)/com.gammonview.helper
rm ~/Library/LaunchAgents/com.gammonview.helper.plist
rm -rf ~/Library/"Application Support"/GammonView
```

`uv` is copied *out* of the app bundle rather than run from it, so the
installer itself is disposable: dragging it to the Trash afterwards — the
normal thing to do with an installer — must not take the working install away.

## Two things that will surprise you

**It looks finished before it is.** ~80 MB of Python, numpy, bgsage and weights
arrive after the window appears, which the plan flags as the one place this
flow can confuse somebody. Hence a sentence that always says what is happening,
and a progress bar that does not stop until something has actually ended.

**A migrating user gets one Keychain prompt.** `keyring` binds a stored item's
ACL to the binary that created it. Somebody who installed the helper by hand
has a token owned by `~/.local/share/uv/tools/...`, and the launcher's copy is
a different binary, so macOS asks once — *Always Allow* is the answer. A new
user has no such item and sees nothing. Changing the manifest's `python` pin
rebuilds the interpreter and can re-ask, which is one more reason that field
moves rarely and deliberately.

## Testing it

A real run takes over `com.gammonview.helper` from whatever is already serving
it, so there is one affordance for testing and it is not a supported interface:

```sh
export GAMMONVIEW_INSTALLER_ROOT=/tmp/gv-test
"build/GammonView Installer.app/Contents/MacOS/GammonViewInstaller"
```

That relocates the install root *and* makes the login-item step a no-op — one
variable, because they are one decision: a sandboxed install is a real install
of everything except the part shared with the rest of the system. Only when it
is set is `GAMMONVIEW_INSTALLER_MANIFEST` honoured as well. That gating is
deliberate: the manifest decides which code lands on somebody's computer, so an
environment variable able to repoint it would be the softest part of the whole
install, and a shipped run has no path to it at all.

The pairing half can be driven without a relay by building a stub wheel that
speaks the two porcelain shapes (`{"event": "offer", ...}` and
`{"event": "linked"}`) and pointing a local manifest at its version.

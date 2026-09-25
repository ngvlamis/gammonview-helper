#!/usr/bin/env bash
# Run the launcher's checks (launcher/macos/Checks/main.swift).
#
# Separate from `build.sh` and much cheaper: it compiles the same sources
# without the universal build, the 35 MB of `uv`, the icon or the signature, so
# it is a few seconds and belongs on every push rather than only on a release.
#
# `Window.swift` and `main.swift` are excluded on purpose -- one needs AppKit
# and the other is top-level code, and neither holds a decision worth checking.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

swiftc -swift-version 5 -o "$OUT/checks" \
    "$HERE/Sources/Installer.swift" \
    "$HERE/Sources/LoginItem.swift" \
    "$HERE/Sources/Manifest.swift" \
    "$HERE/Sources/Paths.swift" \
    "$HERE/Sources/Shell.swift" \
    "$HERE/Checks/main.swift"

"$OUT/checks"

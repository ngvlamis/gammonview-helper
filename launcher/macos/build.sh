#!/usr/bin/env bash
# Build `GammonView Installer.app`.
#
# One command, no Xcode project, no dependencies beyond the Command Line Tools
# and curl. That is deliberate: this artefact is meant to be rebuilt rarely and
# identically, sometimes years apart, and a project file is a thing that rots
# between those occasions.
#
#   launcher/macos/build.sh                 # build into launcher/macos/build/
#   VERSION=1.0.1 launcher/macos/build.sh   # stamp a version
#
# Output: build/GammonView Installer.app, and build/GammonView-Installer.zip
# beside it -- the zip is what gets attached to a release, because a bare .app
# downloaded from a browser arrives as a folder and loses its execute bits.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$HERE/build"
APP="$BUILD/GammonView Installer.app"
VERSION="${VERSION:-1.0.0}"

# Pinned, not `latest`. A frozen installer built twice must be the same
# installer; resolving `latest` at build time means the copy rebuilt next year
# carries a different uv than the one that was tested.
UV_VERSION="${UV_VERSION:-0.12.18}"
UV_BASE="https://github.com/astral-sh/uv/releases/download/$UV_VERSION"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

rm -rf "$BUILD"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$BUILD/uv"

# --- uv, universal ------------------------------------------------------------
#
# Both architectures, lipo'd together. Intel Macs are exactly the machines this
# audience still has: the helper's whole pitch is that an older computer you
# already own can do the analysis overnight.
for arch in aarch64 x86_64; do
    tarball="uv-$arch-apple-darwin.tar.gz"
    say "fetching $tarball"
    curl -fsSL "$UV_BASE/$tarball" -o "$BUILD/uv/$tarball"
    curl -fsSL "$UV_BASE/$tarball.sha256" -o "$BUILD/uv/$tarball.sha256"
    # Verified rather than trusted: this binary is about to be copied onto
    # somebody else's computer and run there.
    (cd "$BUILD/uv" && awk '{print $1"  '"$tarball"'"}' "$tarball.sha256" | shasum -a 256 -c -)
    tar -xzf "$BUILD/uv/$tarball" -C "$BUILD/uv"
    mv "$BUILD/uv/uv-$arch-apple-darwin/uv" "$BUILD/uv/uv-$arch"
done
say "lipo uv"
lipo -create "$BUILD/uv/uv-aarch64" "$BUILD/uv/uv-x86_64" \
     -output "$APP/Contents/Resources/uv"
chmod 755 "$APP/Contents/Resources/uv"

# --- the launcher itself, universal -------------------------------------------
#
# Both architectures, and the Intel one is not optional: the helper's whole
# pitch is that a computer you already own can do the analysis, and the
# computers people already own include Intel Macs.
#
# `-swift-version 5` on purpose. Swift 6's strict concurrency checking is a
# good thing to adopt in software that is maintained; this is software that is
# explicitly not, and the diagnostics it would raise are about a single
# background queue talking to a single window through one `DispatchQueue.main`
# hop. Pinning the language mode also means a future toolchain compiles this
# file the way today's one did, which is the whole ambition for this artefact.
#
# The toolchain hunt below exists for one specific reason. Swift needs
# **back-deployment shim libraries** to target an OS older than the runtime
# features it links against, and the Command Line Tools ship those for arm64
# only -- Apple stopped building the x86_64 slices. A full Xcode still has
# them. So an Intel slice at the floor we want needs Xcode, and a machine with
# only the CLT can still build, at a higher Intel floor, as long as it says so
# rather than quietly shipping something narrower than what was tested.
SOURCES=("$HERE"/Sources/*.swift)
ARM_FLOOR="11.0"
X86_FLOOR="11.0"

probe() {  # $1 = developer dir ("" for whatever is selected), $2 = floor
    local dir="$1" floor="$2" probe_src="$BUILD/probe.swift"
    echo 'print(1)' > "$probe_src"
    if [ -n "$dir" ]; then
        DEVELOPER_DIR="$dir" swiftc -swift-version 5             -target "x86_64-apple-macos$floor" -o "$BUILD/probe" "$probe_src" 2>/dev/null
    else
        swiftc -swift-version 5             -target "x86_64-apple-macos$floor" -o "$BUILD/probe" "$probe_src" 2>/dev/null
    fi
}

TOOLCHAIN=""
for candidate in "${DEVELOPER_DIR:-}" "$(xcode-select -p 2>/dev/null)"                  "/Applications/Xcode.app/Contents/Developer"; do
    [ -d "$candidate" ] || continue
    if probe "$candidate" "$X86_FLOOR"; then TOOLCHAIN="$candidate"; break; fi
done

if [ -z "$TOOLCHAIN" ]; then
    # Nothing available can reach the Intel floor. Fall back, loudly -- and to
    # 13.0 rather than to arm64-only, because dropping a whole architecture
    # silently is how a release goes out that a quarter of the audience cannot
    # open.
    if probe "" "13.0"; then
        X86_FLOOR="13.0"
        cat >&2 <<'WARN'

  ############################################################
  #  NOT THE SHIPPING CONFIGURATION                          #
  #                                                          #
  #  No toolchain here can build an Intel slice for macOS    #
  #  11, so this build raises the Intel floor to macOS 13.    #
  #  Fine for development. For a release, build in CI, or:    #
  #                                                          #
  #    sudo xcodebuild -license accept                        #
  #                                                          #
  ############################################################

WARN
    else
        echo "error: cannot build an x86_64 slice with any available toolchain" >&2
        echo "       install Xcode, or run: sudo xcodebuild -license accept" >&2
        exit 1
    fi
fi

compile() {  # $1 = arch, $2 = target triple arch, $3 = floor
    say "compiling $1 (macOS $3)"
    DEVELOPER_DIR="${TOOLCHAIN:-$(xcode-select -p)}" swiftc -O -swift-version 5 \
        -target "$2-apple-macos$3" \
        -o "$BUILD/GammonViewInstaller-$1" \
        "${SOURCES[@]}"
}
compile arm64 arm64 "$ARM_FLOOR"
compile x86_64 x86_64 "$X86_FLOOR"

say "lipo launcher"
lipo -create "$BUILD/GammonViewInstaller-arm64" "$BUILD/GammonViewInstaller-x86_64" \
     -output "$APP/Contents/MacOS/GammonViewInstaller"

# --- bundle -------------------------------------------------------------------
# `LSMinimumSystemVersion` follows the HIGHER floor. Each slice carries its
# own minimum and the loader honours it, so an Intel Mac below the Intel
# floor would otherwise be refused by dyld with nothing a user can read.
sed -e "s|__VERSION__|$VERSION|g" -e "s|__MIN_OS__|$X86_FLOOR|g" \
    "$HERE/Info.plist" > "$APP/Contents/Info.plist"

say "icon"
ICONSET="$BUILD/icon.iconset"
mkdir -p "$ICONSET"
for size in 16 32 64 128 256 512; do
    sips -z $size $size "$HERE/Resources/icon.png" \
        --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z $double $double "$HERE/Resources/icon.png" \
        --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/icon.icns"

# Ad-hoc signature. This buys nothing against Gatekeeper -- it is not a
# Developer ID and the user still meets the Privacy & Security detour. What it
# buys is the app *running at all* on Apple Silicon, where an unsigned binary
# is killed outright, and a signature that covers the bundle rather than only
# the executable, which is what `--deep` after adding Resources is for.
say "signing (ad-hoc)"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

say "zipping"
# `ditto` rather than `zip`: it preserves the bundle's resource forks and the
# execute bit, which a plain zip does not, and a launcher that arrives
# non-executable is a support email nobody can answer.
ditto -c -k --keepParent "$APP" "$BUILD/GammonView-Installer.zip"

say "built $APP"
du -sh "$APP" "$BUILD/GammonView-Installer.zip" | sed 's/^/    /'

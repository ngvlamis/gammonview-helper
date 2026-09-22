#!/usr/bin/env bash
# Register (or re-register) the helper as a macOS login item.
#
# Idempotent: run it again after upgrading the helper, after changing which
# site it points at, or if you are not sure whether it is loaded. It boots the
# old job out before installing, so a second run never leaves two pollers
# sharing one worker id -- which the relay sees as one machine contradicting
# itself about which job it holds.
#
# Usage:
#   deploy/install-login-item.sh                       # beta, installed helper
#   GAMMONVIEW_SITE=https://gammonview.com deploy/install-login-item.sh
#   HELPER_BIN=/somewhere/else/gammonview-helper deploy/install-login-item.sh
set -euo pipefail

LABEL="com.gammonview.helper"
SITE="${GAMMONVIEW_SITE:-https://beta.gammonview.com}"
HELPER_BIN="${HELPER_BIN:-$HOME/.local/bin/gammonview-helper}"
LOG_DIR="${LOG_DIR:-$HOME/Library/Logs/GammonView}"
AGENTS="$HOME/Library/LaunchAgents"
TEMPLATE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$LABEL.plist"

# Checked rather than assumed: launchd reports a missing program as a generic
# spawn failure in the system log, which is a long way to travel to find a typo.
if [ ! -x "$HELPER_BIN" ]; then
    echo "no helper at $HELPER_BIN" >&2
    echo "install it first:  uv tool install --force $(dirname "$(dirname "$TEMPLATE")")" >&2
    exit 1
fi

# launchd creates the log FILE but not its directory, and a missing directory
# is another silent spawn failure.
mkdir -p "$LOG_DIR" "$AGENTS"

sed -e "s|__HELPER_BIN__|$HELPER_BIN|g" \
    -e "s|__LOG_DIR__|$LOG_DIR|g" \
    -e "s|__SITE__|$SITE|g" \
    "$TEMPLATE" > "$AGENTS/$LABEL.plist"

# `bootout` on a job that is not loaded exits non-zero; that is not a failure
# here, it is the first install.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$AGENTS/$LABEL.plist"

echo "loaded $LABEL -> $SITE"
echo "  binary : $HELPER_BIN"
echo "  log    : $LOG_DIR/helper.log"
echo "  stop   : launchctl bootout gui/$(id -u)/$LABEL"

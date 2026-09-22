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
#   deploy/install-login-item.sh stop                  # stop it, leave it out
#   GAMMONVIEW_SITE=https://gammonview.com deploy/install-login-item.sh
#   HELPER_BIN=/somewhere/else/gammonview-helper deploy/install-login-item.sh
#
# `stop` exists so that stopping the helper does not mean remembering
# `launchctl bootout gui/$(id -u)/com.gammonview.helper`. It also reports
# whether anything was actually loaded, because launchd's own answer for "not
# loaded" is `Boot-out failed: 3: No such process`, which reads like a failure
# and is not one.
#
# Note that installing always leaves the helper RUNNING, so a `stop` does not
# survive the next install -- if you are testing what the site does with no
# helper, do not re-run this in between.
set -euo pipefail

LABEL="com.gammonview.helper"
AGENTS_DIR="$HOME/Library/LaunchAgents"

# What the installed plist already points at, if there is one. Re-running this
# after an upgrade must NOT move a helper between sites -- it is the same
# command you use to pick up a new version, and silently repointing a machine
# that was analysing for gammonview.com at beta (or the reverse) is a helper
# that stops working for reasons nobody can see. So an existing target wins
# over the default, and only an explicit GAMMONVIEW_SITE overrides it.
INSTALLED_SITE=""
if [ -f "$AGENTS_DIR/$LABEL.plist" ]; then
    INSTALLED_SITE="$(sed -n 's|.*<string>\(https\{0,1\}://[^<]*\)</string>.*|\1|p' \
        "$AGENTS_DIR/$LABEL.plist" | head -1)"
fi

SITE="${GAMMONVIEW_SITE:-${INSTALLED_SITE:-https://beta.gammonview.com}}"
HELPER_BIN="${HELPER_BIN:-$HOME/.local/bin/gammonview-helper}"
ACTION="${1:-install}"
LOG_DIR="${LOG_DIR:-$HOME/Library/Logs/GammonView}"
AGENTS="$AGENTS_DIR"
TEMPLATE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$LABEL.plist"

if [ "$ACTION" = "stop" ]; then
    if launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null; then
        echo "stopped $LABEL"
    else
        echo "$LABEL was not loaded"
    fi
    # Said because the site will disagree for a while: a helper stops by going
    # quiet, so the relay waits WORKER_FRESH_SECONDS (120) before it calls the
    # machine anything but connected.
    echo "  the website keeps saying Connected for up to 2 minutes"
    echo "  start again: $0"
    exit 0
elif [ "$ACTION" != "install" ]; then
    echo "usage: $0 [install|stop]" >&2
    exit 2
fi

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

if [ -n "$INSTALLED_SITE" ] && [ "$INSTALLED_SITE" != "$SITE" ]; then
    echo "note: moved from $INSTALLED_SITE to $SITE"
    echo "      this machine must be linked to the new site separately"
fi
echo "loaded $LABEL -> $SITE"
echo "  binary : $HELPER_BIN"
echo "  log    : $LOG_DIR/helper.log"
echo "  stop   : launchctl bootout gui/$(id -u)/$LABEL"

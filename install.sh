#!/usr/bin/env bash
# Install or uninstall niri-agent for the current user.
#   ./install.sh             install
#   ./install.sh --uninstall remove symlinks, agents.kdl and the include line
set -euo pipefail

REPO=$(cd "$(dirname "$0")" && pwd)
BIN_DIR=${XDG_BIN_HOME:-$HOME/.local/bin}
NIRI_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/niri
SKILLS_DIR=${NIRI_AGENT_SKILLS_DIR:-$HOME/.agents/skills}
CONFIG=$NIRI_DIR/config.kdl
INCLUDE_LINE='include "agents.kdl"'

if [[ ${1:-} == --uninstall ]]; then
    if command -v niri-agent >/dev/null && [[ -n ${NIRI_SOCKET:-} ]]; then
        niri-agent stop --all >/dev/null || true
    fi
    rm -f "$BIN_DIR/niri-agent" "$SKILLS_DIR/niri-agent"
    if [[ -f $CONFIG ]] && grep -qxF "$INCLUDE_LINE" "$CONFIG"; then
        cp "$CONFIG" "$CONFIG.bak-niri-agent-$(date +%s)"
        sed -i "\\|^${INCLUDE_LINE}\$|d" "$CONFIG"
    fi
    rm -f "$NIRI_DIR/agents.kdl"
    echo "niri-agent uninstalled"
    exit 0
fi

for cmd in niri python3 dbus-daemon; do
    command -v "$cmd" >/dev/null || { echo "missing required command: $cmd" >&2; exit 1; }
done
[[ -f $CONFIG ]] || { echo "niri config not found at $CONFIG" >&2; exit 1; }

mkdir -p "$BIN_DIR" "$SKILLS_DIR"
ln -sfn "$REPO/niri_agent.py" "$BIN_DIR/niri-agent"
ln -sfn "$REPO/skill/niri-agent" "$SKILLS_DIR/niri-agent"

[[ -e $NIRI_DIR/agents.kdl ]] || cp "$REPO/config/agents.kdl" "$NIRI_DIR/agents.kdl"

if ! grep -qxF "$INCLUDE_LINE" "$CONFIG"; then
    cp "$CONFIG" "$CONFIG.bak-niri-agent-$(date +%s)"
    # Appended last so the transient launch rule wins over earlier window rules.
    printf '\n%s\n' "$INCLUDE_LINE" >>"$CONFIG"
fi

niri validate -c "$CONFIG" >/dev/null 2>&1 || { echo "niri validate failed for $CONFIG" >&2; exit 1; }

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "note: $BIN_DIR is not on PATH" ;;
esac
echo "niri-agent installed: $BIN_DIR/niri-agent, skill at $SKILLS_DIR/niri-agent"

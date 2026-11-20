#!/usr/bin/env bash
# Verify BT + mpv audio — delegates to playerctl verify
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/rpi-audio-player}"

[[ "${EUID}" -eq 0 ]] || { echo "run as root: sudo $0 ..." >&2; exit 1; }

args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --test-tone) args+=(--test-tone); shift ;;
        --require-playback) args+=(--require-playback); shift ;;
        --json) args+=(--json); shift ;;
        -h|--help)
            cat <<'EOF'
Usage: audio_verify.sh [--test-tone] [--require-playback] [--json]
EOF
            exit 0
            ;;
        *) echo "unknown: $1" >&2; exit 1 ;;
    esac
done

exec python3 "${INSTALL_DIR}/scripts/playerctl.py" verify "${args[@]}"
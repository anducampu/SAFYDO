#!/usr/bin/env bash
# Test mpv playback to Bluetooth A2DP sink at best available codec
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

URL=""
DURATION=30
MAC=""

usage() {
    cat <<'EOF'
Usage: play_test.sh [options] [URL]

Play a test stream to the connected BT device via mpv + bluealsa.
Default: first stream from config/streams.json

Options:
  --mac MAC        Bluetooth device (default: primary_mac)
  --duration SEC   Playback length (default: 30, 0 = until end)
  -h, --help       Show help
EOF
}

default_stream_url() {
    python3 - <<'PY'
import json
from pathlib import Path
p = Path("/opt/rpi-audio-player/config/streams.json")
data = json.loads(p.read_text())
streams = data.get("streams") or []
print(streams[0]["url"] if streams else "")
PY
}

main() {
    require_cmd mpv python3

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --mac) MAC="$2"; shift 2 ;;
            --duration) DURATION="$2"; shift 2 ;;
            -h|--help) usage; exit 0 ;;
            -*) die "unknown option: $1" ;;
            *) URL="$1"; shift ;;
        esac
    done

    MAC="${MAC:-$(primary_mac || true)}"
    [[ -n "$MAC" ]] || die "no MAC — pair a device first (bt_pair.sh)"

    if ! bt_device_connected "$MAC"; then
        log "not connected — connecting ${MAC}"
        "${SCRIPT_DIR}/bt_connect.sh" "$MAC"
    fi

    URL="${URL:-$(default_stream_url)}"
    [[ -n "$URL" ]] || die "no URL provided and streams.json is empty"

    local audio_dev
    audio_dev="$(bt_mpv_audio_device "$MAC")"
    local volume
    volume="$(cfg_get audio.volume 2>/dev/null || echo 80)"

    log "Playing to ${audio_dev}"
    log "URL: ${URL}"

    local -a mpv_args=(
        --no-video
        --volume="$volume"
        --audio-device="$audio_dev"
        --msg-level=all=v
    )
    if [[ "$DURATION" -gt 0 ]]; then
        mpv_args+=(--length="$DURATION")
    fi
    mpv_args+=("$URL")

    # Brief playback primes A2DP so codec detection is accurate
    if ! mpv "${mpv_args[@]}"; then
        die "mpv playback failed"
    fi

    "${SCRIPT_DIR}/bt_codec.sh" --update-status "$MAC"
    log "Playback test complete — see ${BT_STATUS_FILE}"
}

[[ "${EUID}" -eq 0 ]] || die "run as root: sudo $0 ..."
main "$@"
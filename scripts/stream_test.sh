#!/usr/bin/env bash
# Test stream playback — tries favorites first, then all streams
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

SECONDS_EACH=20
FAVORITES_ONLY=true
STREAM_ID=""

usage() {
    cat <<'EOF'
Usage: stream_test.sh [options] [stream_id]

Play streams via playerctl + mpv IPC to test BT audio.

Options:
  --seconds N        Seconds per stream (default: 20)
  --all              Test all streams (default: favorites only)
  --probe URL        Only check HTTP reachability, no playback
  -h, --help         Show help

Examples:
  sudo ./scripts/stream_test.sh
  sudo ./scripts/stream_test.sh amsterdam-funk-channel
  sudo ./scripts/stream_test.sh --all --seconds 15
EOF
}

probe_url() {
    local url="$1"
    if curl -fsS --connect-timeout 8 -o /dev/null "$url" 2>/dev/null; then
        echo "OK"
        return 0
    fi
    local code
    code="$(curl -s --connect-timeout 8 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || echo 000)"
    echo "$code"
    return 1
}

play_one() {
    local sid="$1"
    log "Playing stream: ${sid} (${SECONDS_EACH}s)"
    if ! python3 "${SCRIPT_DIR}/playerctl.py" play-stream "$sid" >/dev/null; then
        warn "play-stream failed: ${sid}"
        return 1
    fi
    sleep "$SECONDS_EACH"
    python3 "${SCRIPT_DIR}/playerctl.py" stop >/dev/null 2>&1 || true
    "${SCRIPT_DIR}/bt_codec.sh" --update-status 2>/dev/null || true
    return 0
}

main() {
    require_cmd python3 curl
    systemctl is-active audio-player-mpv >/dev/null 2>&1 || {
        die "audio-player-mpv not active — run: sudo systemctl start audio-player-mpv"
    }

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --seconds) SECONDS_EACH="$2"; shift 2 ;;
            --all) FAVORITES_ONLY=false; shift ;;
            --probe) probe_url "$2"; exit $? ;;
            -h|--help) usage; exit 0 ;;
            -*) die "unknown option: $1" ;;
            *) STREAM_ID="$1"; shift ;;
        esac
    done

    python3 "${SCRIPT_DIR}/import_stations.py" --install-dir "${INSTALL_DIR}" 2>/dev/null || true

    if [[ -n "$STREAM_ID" ]]; then
        play_one "$STREAM_ID"
        exit $?
    fi

    local fav_py="False"
    if $FAVORITES_ONLY; then
        fav_py="True"
    fi

    # "id<TAB>url" in one pass. It used to re-run playerctl.py (the whole
    # backend import) once per stream, and splice each id into Python source.
    local -a ids=() urls=()
    local sid url code
    while IFS=$'\t' read -r sid url; do
        [[ -n "$sid" ]] || continue
        ids+=("$sid")
        urls+=("$url")
    done < <(python3 - "${INSTALL_DIR}/config/streams.json" "$fav_py" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
fav_only = sys.argv[2] == "True"
for s in data.get("streams") or []:
    if fav_only and not s.get("favorite"):
        continue
    print(f"{s['id']}\t{s.get('url') or ''}")
PY
)

    [[ ${#ids[@]} -gt 0 ]] || die "no streams to test"

    log "Testing ${#ids[@]} stream(s)..."
    local i
    for i in "${!ids[@]}"; do
        sid="${ids[$i]}"
        url="${urls[$i]}"
        [[ -n "$url" ]] || continue
        code="$(probe_url "$url" || true)"
        log "${sid}: reachability=${code}"
        if [[ "$code" == "OK" || "$code" =~ ^[23] ]]; then
            play_one "$sid" && { log "SUCCESS: ${sid}"; exit 0; }
        fi
    done
    die "no stream played successfully — check Pi internet/routing"
}

[[ "${EUID}" -eq 0 ]] || die "run as root: sudo $0 ..."
main "$@"
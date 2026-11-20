#!/usr/bin/env bash
# Post-reboot / post-deploy smoke test for Phase 6
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

PI_HOST="${PI_HOST:-safydo.local}"
FAIL=0

check() {
    local name="$1"
    shift
    printf '%-28s' "$name"
    if "$@" >/dev/null 2>&1; then
        echo "OK"
        return 0
    fi
    echo "FAIL"
    FAIL=$((FAIL + 1))
    return 1
}

curl_local() {
    curl -sf -m 10 "http://127.0.0.1$1" 2>/dev/null || curl -sf -m 10 "http://127.0.0.1:8000$1"
}

curl_local_post() {
    curl -sf -m 15 -X POST "http://127.0.0.1$1" 2>/dev/null || curl -sf -m 15 -X POST "http://127.0.0.1:8000$1"
}

main() {
    echo "=== Phase 6 smoke test ==="
    echo "host: $(hostname)  $(date -Iseconds)"
    echo ""

    check "bluetooth.service" systemctl is-active bluetooth
    check "bluealsa.service" systemctl is-active bluealsa
    check "audio-player-mpv" systemctl is-active audio-player-mpv
    check "audio-player-backend" systemctl is-active audio-player-backend
    check "nginx" systemctl is-active nginx
    check "mpv IPC socket" test -S /run/audio-player/mpv.sock
    check "API health" curl_local /api/health
    check "mDNS ping" ping -c1 -W2 "${PI_HOST}"

    local mac bt_ok stream_ok
    mac="$(primary_mac || true)"
    if [[ -n "$mac" ]]; then
        check "BT connected (${mac})" bt_device_connected "$mac"
        if bt_device_connected "$mac" 2>/dev/null; then
            "${SCRIPT_DIR}/bt_codec.sh" --update-status "$mac" >/dev/null 2>&1 || true
        fi
    else
        printf '%-28s' "BT primary_mac"
        echo "SKIP"
    fi

    if curl_local /api/status | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('audio_check',{}).get('ok',True) else 1)" 2>/dev/null; then
        printf '%-28s' "audio_check"
        echo "OK"
    else
        printf '%-28s' "audio_check"
        echo "WARN"
    fi

    if [[ "${SMOKE_PLAYBACK:-0}" == "1" ]]; then
        if curl_local_post /api/play/stream/jazz24 | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('mpv',{}).get('idle') is False else 1)" 2>/dev/null; then
            printf '%-28s' "stream playback"
            echo "OK"
            curl_local_post /api/transport/stop >/dev/null 2>&1 || true
        else
            printf '%-28s' "stream playback"
            echo "FAIL"
            FAIL=$((FAIL + 1))
        fi
    else
        printf '%-28s' "stream playback"
        echo "SKIP (set SMOKE_PLAYBACK=1 to test)"
    fi

    if command -v free >/dev/null 2>&1; then
        local used_mb
        used_mb="$(free -m | awk '/^Mem:/{print $3}')"
        printf '%-28s' "RAM used"
        echo "${used_mb} MB"
    fi

    echo ""
    if [[ $FAIL -eq 0 ]]; then
        log "Phase 6 smoke: PASS"
        exit 0
    fi
    warn "Phase 6 smoke: ${FAIL} failure(s)"
    exit 1
}

main "$@"
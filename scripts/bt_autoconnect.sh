#!/usr/bin/env bash
# Auto-connect known Bluetooth audio devices.
#   boot (oneshot unit): pick the best candidate and connect
#   --retry (timer unit): no-op while connected, else same selection
# bluetooth.autoconnect_strategy: strongest (RSSI during a short scan) | primary
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

AUTO_RECONNECT="${AUTO_RECONNECT:-true}"
RETRY_MODE=false
RSSI_SCAN_SECS="${RSSI_SCAN_SECS:-8}"

# Prints "MAC yes|no" (connected state) for paired devices with an audio
# sink profile — one bluetoothctl info call per device.
scan_candidates() {
    local mac name info conn
    while read -r _ mac name; do
        [[ -n "${mac:-}" ]] || continue
        info="$(bluetoothctl info "$mac" 2>/dev/null || true)"
        echo "$info" | grep -qiE 'Icon: audio-|0000110b' || continue
        conn=no
        echo "$info" | grep -qE '^[[:space:]]*Connected: yes' && conn=yes
        echo "$mac $conn"
    done < <(bluetoothctl devices Paired 2>/dev/null)
}

# Sample RSSI for the given MACs during a short discovery window. Prints
# "MAC RSSI" for each device seen; non-discoverable devices print nothing.
sample_rssi() {
    local secs="$1"; shift
    local -a macs=("$@")
    local scanner deadline mac r
    declare -A seen=()
    bluetoothctl --timeout "$secs" scan on >/dev/null 2>&1 &
    scanner=$!
    deadline=$((SECONDS + secs))
    while (( SECONDS < deadline && ${#seen[@]} < ${#macs[@]} )); do
        for mac in "${macs[@]}"; do
            [[ -n "${seen[$mac]:-}" ]] && continue
            # "RSSI: -55" (bluez 5.66) or "RSSI: 0x.. (-55)" (newer)
            r="$(bluetoothctl info "$mac" 2>/dev/null | awk '/RSSI:/{
                if (match($0, /\(-?[0-9]+\)/)) print substr($0, RSTART+1, RLENGTH-2);
                else print $2; exit }')"
            if [[ "$r" =~ ^-?[0-9]+$ ]]; then
                seen[$mac]="$r"
                echo "$mac $r"
            fi
        done
        sleep 0.5
    done
    kill "$scanner" 2>/dev/null || true
    wait "$scanner" 2>/dev/null || true
}

last_connected_epoch() {
    local mac="$1"
    [[ -f "$KNOWN_DEVICES_FILE" ]] || { echo 0; return; }
    jq -r --arg mac "$mac" '.devices[$mac].last_connected_epoch // 0' \
        "$KNOWN_DEVICES_FILE" 2>/dev/null || echo 0
}

# The mpv sink follows bluetooth.primary_mac (mpv_daemon.sh resolves it at
# startup), so whichever device we connect must become primary. At boot mpv
# starts after this unit; when run later, re-point a running mpv.
sync_primary() {
    local mac="$1" primary
    primary="$(primary_mac || true)"
    if [[ "$primary" != "$mac" ]]; then
        cfg_set "bluetooth.primary_mac" "$mac"
        log "primary_mac -> ${mac}"
        mpv_restart_now "sink -> ${mac}"
        return 0
    fi
    # Primary unchanged is NOT the same as "nothing to do": mpv exits when the
    # link was down at its start, so the socket can still be missing here. This
    # is the common path (device reconnects, primary already correct) and used
    # to return early, leaving playback 503ing until systemd's slow restart
    # cycle happened to land after the link came up.
    mpv_ensure_running "primary ${mac} connected"
}

# Retry ticks must not fight a user-initiated pair/connect: honor the
# pairing sentinel (the pair phase holds no lock), then probe the
# bluetoothctl lock non-blocking and RELEASE it before connecting
# (holding it would deadlock bt_connect.sh's own flock).
# Kept as a thin alias: the implementation now lives in _common.sh so the wedge
# watchdog backs off on exactly the same conditions this does.
retry_busy() { bt_ops_busy; }

main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --retry) RETRY_MODE=true; shift ;;
            -h|--help)
                echo "Usage: bt_autoconnect.sh [--retry]"
                exit 0
                ;;
            *) die "unknown option: $1" ;;
        esac
    done

    # Either switch turns it off. It used to take both (`&&`), and the env one
    # defaults to true, so `auto_reconnect: false` in player.yaml did nothing.
    local auto_flag
    auto_flag="$(cfg_get bluetooth.auto_reconnect 2>/dev/null || echo true)"
    auto_flag="${auto_flag,,}"
    if [[ "$auto_flag" =~ ^(false|0|no)$ || "${AUTO_RECONNECT,,}" != "true" ]]; then
        log "auto_reconnect disabled — skipping"
        exit 0
    fi

    require_cmd bluetoothctl
    if $RETRY_MODE; then
        # Before bt_ensure_ready, not after: that is four bluetoothctl launches
        # (power, pairable, agent, default-agent), exactly the agent churn a
        # pairing in flight must not see.
        if retry_busy; then
            log "bluetoothctl busy (pair/connect in flight) — skipping retry"
            exit 0
        fi
        if ! bluetoothctl show >/dev/null 2>&1; then
            # Never fail the timer unit on a missing adapter
            log "adapter unavailable — skipping retry"
            exit 0
        fi
    fi
    bt_ensure_ready

    if ! $RETRY_MODE; then
        # Boot: wait for the adapter to power up
        local wait=0
        while [[ $wait -lt 30 ]]; do
            if bluetoothctl show 2>/dev/null | grep -q "Powered: yes"; then
                break
            fi
            sleep 1
            wait=$((wait + 1))
        done
    fi

    # Gather paired audio devices (+ connected state, one info pass)
    local -a candidates=() connected_macs=()
    local connected="" mac conn
    while read -r mac conn; do
        [[ -n "${mac:-}" ]] || continue
        candidates+=("$mac")
        [[ "$conn" == "yes" ]] && connected_macs+=("$mac")
    done < <(scan_candidates)
    # Prefer the current primary when several devices are connected at once —
    # never steal primary (and restart mpv) from a speaker that is playing.
    if [[ ${#connected_macs[@]} -gt 0 ]]; then
        connected="${connected_macs[0]}"
        local cur_primary
        cur_primary="$(primary_mac || true)"
        for mac in "${connected_macs[@]}"; do
            [[ "$mac" == "$cur_primary" ]] && { connected="$mac"; break; }
        done
    fi

    if [[ ${#candidates[@]} -eq 0 ]]; then
        mac="$(primary_mac || true)"
        if [[ -z "$mac" ]]; then
            log "no known audio devices — skipping"
            exit 0
        fi
        candidates=("$mac")
    fi

    if [[ -n "$connected" ]] && bt_a2dp_missing "$connected"; then
        # "Connected" with no audio stream: the retry used to log "already
        # connected" every 45 s and leave the speaker silent indefinitely.
        log "connected without an A2DP stream: ${connected} — reconnecting the audio profile"
        if "${SCRIPT_DIR}/bt_connect.sh" "$connected"; then
            sync_primary "$connected"
        fi
        exit 0
    fi
    if [[ -n "$connected" ]]; then
        log "already connected: ${connected}"
        if ! $RETRY_MODE; then
            "${SCRIPT_DIR}/bt_codec_prime.sh" "$connected" || true
        fi
        # After priming, for the same PCM-contention reason as bt_pair.sh.
        sync_primary "$connected"
        exit 0
    fi

    # Order candidates
    local strategy
    strategy="$(cfg_get bluetooth.autoconnect_strategy 2>/dev/null || echo strongest)"
    [[ -n "$strategy" ]] || strategy=strongest

    local primary epoch
    primary="$(primary_mac || true)"
    local -a ordered=()
    if [[ "$strategy" == "strongest" && ${#candidates[@]} -gt 1 ]]; then
        declare -A rssi_of=()
        local r
        log "sampling RSSI for ${#candidates[@]} candidates (${RSSI_SCAN_SECS}s max)..."
        while read -r mac r; do
            rssi_of[$mac]="$r"
        done < <(sample_rssi "$RSSI_SCAN_SECS" "${candidates[@]}")
        # seen-in-scan first (RSSI desc), then unseen by last-connected desc,
        # primary as final tiebreak
        while read -r mac; do
            ordered+=("$mac")
        done < <(
            for mac in "${candidates[@]}"; do
                local seen=1 rv=-999 prim=1
                if [[ -n "${rssi_of[$mac]:-}" ]]; then seen=0; rv="${rssi_of[$mac]}"; fi
                [[ "$mac" == "$primary" ]] && prim=0
                epoch="$(last_connected_epoch "$mac")"
                printf '%d %d %d %d %s\n' "$seen" "$((-rv))" "$((-epoch))" "$prim" "$mac"
                log "  ${mac} rssi=${rssi_of[$mac]:-none} last_epoch=${epoch}"
            done | sort -k1,1n -k2,2n -k3,3n -k4,4n | awk '{print $NF}'
        )
    else
        # primary first, then last-connected desc
        while read -r mac; do
            ordered+=("$mac")
        done < <(
            for mac in "${candidates[@]}"; do
                local prim=1
                [[ "$mac" == "$primary" ]] && prim=0
                epoch="$(last_connected_epoch "$mac")"
                printf '%d %d %s\n' "$prim" "$((-epoch))" "$mac"
            done | sort -k1,1n -k2,2n | awk '{print $NF}'
        )
    fi

    for mac in "${ordered[@]}"; do
        log "auto-connecting ${mac}"
        if "${SCRIPT_DIR}/bt_connect.sh" "$mac"; then
            sync_primary "$mac"
            exit 0
        fi
    done

    if $RETRY_MODE; then
        log "no known device reachable — next retry per timer"
    else
        warn "auto-connect failed — retry timer will keep trying"
    fi
    exit 0
}

[[ "${EUID}" -eq 0 ]] || die "run as root: sudo $0 ..."
main "$@"

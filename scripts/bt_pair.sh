#!/usr/bin/env bash
# Bluetooth pairing — discover, pair, trust, connect, save primary device
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

SCAN_SECS=25
SET_PRIMARY=true

usage() {
    cat <<'EOF'
Usage: bt_pair.sh <command> [options]

Commands:
  scan [--seconds N]     Discover nearby devices (default 25s)
  list                   List known (paired) devices
  pair <MAC>             Pair, trust, connect, set as primary
  remove <MAC>           Unpair device

Options:
  --no-primary           Do not update bluetooth.primary_mac in player.yaml
  -h, --help             Show help

Examples:
  sudo ./scripts/bt_pair.sh scan
  sudo ./scripts/bt_pair.sh pair AA:BB:CC:DD:EE:FF
EOF
}

run_scan() {
    bt_ensure_ready
    log "Scanning for ${SCAN_SECS}s — put speaker/headphones in pairing mode"
    # BlueZ stops discovery the moment the requesting client exits, so keep
    # one bluetoothctl alive for the whole window (also resolves device names).
    timeout "$((SCAN_SECS + 10))" bluetoothctl --timeout "$SCAN_SECS" scan on >/dev/null 2>&1 || true
    echo ""
    echo "Discovered / known devices:"
    # Plain "MAC NAME" lines: paired/connected state is added by the
    # backend's batched enrichment; per-device bluetoothctl info calls
    # here cost ~0.3 s each on a Pi Zero 2W. Use `bt_pair.sh list` for
    # the paired/connected view on the CLI.
    bluetoothctl devices | while read -r _ mac name; do
        printf '  %s  %s\n' "$mac" "$name"
    done
}

run_list() {
    bt_ensure_ready
    bluetoothctl devices Paired | while read -r _ mac name; do
        local state=""
        bt_device_connected "$mac" && state=" [CONNECTED]"
        printf '  %s  %s%s\n' "$mac" "$name" "$state"
    done
}

# Fast path: one-shot bluetoothctl pair blocks on the D-Bus Pair() reply
# (event-driven, 2-5 s for Just Works speakers) instead of fixed sleeps.
bt_pair_fast() {
    local mac="$1"
    local logfile="/tmp/bt_pair_${mac//:/_}.log"
    bt_wait_device_visible "$mac" 15 || warn "device not seen in scan — trying pair anyway"
    timeout 30 bluetoothctl pair "$mac" >"$logfile" 2>&1 || true
    if grep -qE "Pairing successful|AlreadyExists" "$logfile"; then
        return 0
    fi
    local i
    for i in {1..6}; do
        bt_device_paired "$mac" && return 0
        sleep 0.5
    done
    return 1
}

# Legacy path: interactive session with a live NoInputNoOutput agent, for
# devices that need PIN/authorization (the one-shot pair would stall there).
bt_pair_session_legacy() {
    local mac="$1"
    local logfile="/tmp/bt_pair_${mac//:/_}.log"
    local pre_scan=10
    # Device already discovered/known by BlueZ? Skip the pre-pair scan.
    if bluetoothctl devices 2>/dev/null | grep -qi "^Device ${mac}"; then
        pre_scan=0
    fi
    killall bluetoothctl 2>/dev/null || true
    sleep 1
    (
        echo "power on"
        echo "agent NoInputNoOutput"
        echo "default-agent"
        if [[ $pre_scan -gt 0 ]]; then
            echo "scan on"
            sleep "$pre_scan"
        fi
        echo "pair ${mac}"
        sleep 8
        echo "trust ${mac}"
        sleep 2
        echo "connect ${mac}"
        sleep 6
        echo "quit"
    ) | bluetoothctl 2>&1 | tee "$logfile"
    if grep -qE "Pairing successful|AlreadyExists" "$logfile"; then
        return 0
    fi
    if bt_device_paired "$mac"; then
        return 0
    fi
    return 1
}

pair_device() {
    local mac="$1"
    mac="$(normalize_mac "$mac")" || die "invalid MAC: $1"

    bt_ensure_ready
    # Mark pairing in progress so the retry timer skips its tick — the pair
    # phase does not hold btctl.lock (only bt_connect_session does).
    mkdir -p "$RUN_DIR"
    : >"${RUN_DIR}/pairing.active"
    trap 'rm -f "${RUN_DIR}/pairing.active"' EXIT
    # Stop any background discovery (UI live scan, retry RSSI sampler) —
    # scanning during pair/connect is a known flakiness source. All scan
    # spawns share the "bluetoothctl --timeout" command shape; keep them
    # in sync with this pattern (also spawned in _common.sh, bluetooth.py,
    # bt_autoconnect.sh).
    pkill -f "bluetoothctl --timeout" 2>/dev/null || true
    # Also clear any stray bluetoothctl still holding the pairing agent. A
    # leftover session makes the next `agent` registration fail ("Failed to
    # register agent object"); with no SSP agent available BlueZ falls back to
    # a legacy PIN request that our NoInputNoOutput flow answers with the wrong
    # thing → org.bluez.Error.AuthenticationFailed even on Just-Works speakers.
    # All bluetoothctl use in this project is one-shot, so a blanket kill right
    # before pairing is safe; the 1 s lets D-Bus release the agent object path.
    killall bluetoothctl 2>/dev/null || true
    sleep 1
    bluetoothctl agent NoInputNoOutput >/dev/null 2>&1 || true
    bluetoothctl default-agent >/dev/null 2>&1 || true
    log "Pairing ${mac}..."
    if bt_device_paired "$mac"; then
        log "Already paired — connecting ${mac}..."
        bt_connect_session "$mac" || die "connect failed for ${mac} — power on speaker, disconnect from phone"
    else
        if bt_pair_fast "$mac"; then
            log "Paired via fast path"
        else
            warn "fast pair failed — falling back to interactive agent session"
            bt_pair_session_legacy "$mac" || die "pair failed for ${mac} — ensure device is in pairing mode"
        fi
        bt_connect_session "$mac" || die "device not connected after pairing"
    fi
    bt_device_connected "$mac" || die "device not connected"

    # Connected is not enough: after a bluealsa rebuild, stale BlueZ endpoint
    # caches leave the link up with no A2DP playback transport ("Unable to
    # select SEP"). Heal once and reconnect — mirrors bt_connect.sh.
    if ! bt_wait_a2dp_pcm "$mac" 12; then
        warn "connected but no A2DP playback transport"
        bluetoothctl disconnect "$mac" >/dev/null 2>&1 || true
        # Refused for an unbonded speaker; a plain reconnect is still worth it.
        bt_clear_endpoint_cache "$mac" || true
        bt_connect_session "$mac" || die "reconnect after endpoint-cache heal failed for ${mac}"
        bt_wait_a2dp_pcm "$mac" 12 || warn "A2DP transport still missing — power-cycle the device and retry"
    fi

    if $SET_PRIMARY; then
        cfg_set "bluetooth.primary_mac" "$mac"
        log "Saved primary device: ${mac}"
    fi

    local name
    name="$(bt_device_name "$mac")"
    log "Paired and connected: ${name} (${mac})"
    bt_mark_connected "$mac" "$name" || true
    "${SCRIPT_DIR}/bt_codec_prime.sh" "$mac" || true
    # Point the player at the new sink — after priming, so the daemon and
    # bt_codec_prime.sh never contend for the same BlueALSA PCM. mpv resolves
    # primary_mac at startup only and may be down entirely (it gives up when
    # nothing was connected at boot), so a successful pair otherwise still
    # left play returning 503.
    if $SET_PRIMARY; then
        mpv_restart_now "paired ${mac}"
    fi
}

remove_device() {
    local mac="$1"
    mac="$(normalize_mac "$mac")" || die "invalid MAC: $1"
    bt_ensure_ready
    bluetoothctl disconnect "$mac" >/dev/null 2>&1 || true
    bluetoothctl remove "$mac" || die "remove failed for ${mac}"
    local primary
    primary="$(primary_mac || true)"
    if [[ "$primary" == "$mac" ]]; then
        cfg_set "bluetooth.primary_mac" ""
        log "Cleared primary_mac"
    fi
    bt_known_forget "$mac" || true
    # A removed device can't be connected — rewrite bt_status.json so the UI
    # doesn't keep showing a ghost connection. Only when the file points at
    # THIS device: rewriting for an unrelated MAC would clobber the live
    # status of a different, still-connected speaker.
    local status_mac=""
    [[ -f "$BT_STATUS_FILE" ]] && status_mac="$(jq -r '.device_mac // empty' "$BT_STATUS_FILE" 2>/dev/null || true)"
    if [[ "$status_mac" == "$mac" ]]; then
        "${SCRIPT_DIR}/bt_codec.sh" --update-status "$mac" >/dev/null 2>&1 || true
    fi
    log "Removed ${mac}"
}

parse_args() {
    [[ $# -gt 0 ]] || { usage; exit 1; }
    case "$1" in
        -h|--help) usage; exit 0 ;;
        scan)
            shift
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --seconds) SCAN_SECS="$2"; shift 2 ;;
                    *) die "unknown scan option: $1" ;;
                esac
            done
            run_scan
            ;;
        list)
            run_list
            ;;
        pair)
            shift
            # Every flag first, then act: pairing on the first non-flag meant
            # `pair MAC --no-primary` paired with the flag never read.
            local target=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --no-primary) SET_PRIMARY=false; shift ;;
                    *) [[ -z "$target" ]] || die "pair takes one MAC"; target="$1"; shift ;;
                esac
            done
            [[ -n "$target" ]] || die "pair requires MAC"
            pair_device "$target"
            exit 0
            ;;
        remove)
            shift
            [[ $# -ge 1 ]] || die "remove requires MAC"
            remove_device "$1"
            ;;
        *)
            die "unknown command: $1"
            ;;
    esac
}

[[ "${EUID}" -eq 0 ]] || die "run as root: sudo $0 ..."
parse_args "$@"
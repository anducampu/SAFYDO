#!/usr/bin/env bash
# Connect to primary (or specified) Bluetooth audio device
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

MAC=""
# BT_CONNECT_RETRIES=1 is set by the retry timer so an unreachable device
# doesn't stretch a background tick to minutes.
RETRIES="${BT_CONNECT_RETRIES:-3}"

usage() {
    cat <<'EOF'
Usage: bt_connect.sh [MAC]

Connect paired A2DP device. Uses bluetooth.primary_mac when MAC omitted.
EOF
}

connect_mac() {
    local mac="$1"
    mac="$(normalize_mac "$mac")" || die "invalid MAC: $1"

    bt_ensure_ready

    if ! bt_device_paired "$mac"; then
        die "device not paired: ${mac} (run bt_pair.sh pair first)"
    fi

    if ! bt_device_connected "$mac" && ! bt_device_reachable "$mac"; then
        local name
        name="$(bt_device_name "$mac")"
        bluetoothctl show 2>/dev/null | grep -qE '^[[:space:]]*Powered: yes' || \
            die "Bluetooth adapter is not powered — cannot connect ${name:-$mac}"
        die "${name:-$mac} isn't answering — turn it on, or disconnect it from the phone or computer holding it"
    fi

    local attempt=1 healed=false
    while [[ $attempt -le $RETRIES ]]; do
        log "Connect attempt ${attempt}/${RETRIES} -> ${mac}"
        if bt_connect_session "$mac"; then
            if ! bt_wait_a2dp_pcm "$mac" 12; then
                warn "connected but no A2DP playback transport"
                if ! $healed; then
                    healed=true
                    bluetoothctl disconnect "$mac" >/dev/null 2>&1 || true
                    # Refused for an unbonded speaker: then just reconnect.
                    bt_clear_endpoint_cache "$mac" || sleep 2
                    continue
                fi
            fi
            local name
            name="$(bt_device_name "$mac")"
            log "Connected: ${name} (${mac})"
            bt_mark_connected "$mac" "$name" || true
            "${SCRIPT_DIR}/bt_codec_prime.sh" "$mac" || true
            return 0
        fi
        sleep 2
        attempt=$((attempt + 1))
    done

    local name
    name="$(bt_device_name "$mac")"

    # A dead adapter fails every attempt identically, and nothing done to the
    # speaker will help — so say which of the two is actually broken. Naming
    # some other device here (it used to hardcode one) sends people to the
    # wrong end of the problem entirely.
    if ! bluetoothctl show 2>/dev/null | grep -qE '^[[:space:]]*Powered: yes'; then
        die "Bluetooth adapter is not powered — cannot connect ${name:-$mac}.
  Check:  rfkill list           (a soft block just needs 'rfkill unblock bluetooth')
          dmesg | grep hci0     ('Opcode 0x0c03 failed: -110' = controller wedged; reboot)"
    fi

    die "failed to connect ${name:-$mac} (${mac}) — power the device on, hold its Bluetooth button until it is in pairing mode, and disconnect it from any phone still holding it"
}

[[ "${EUID}" -eq 0 ]] || die "run as root: sudo $0 ..."
[[ "${1:-}" != "-h" && "${1:-}" != "--help" ]] || { usage; exit 0; }

MAC="${1:-$(primary_mac || true)}"
[[ -n "$MAC" ]] || die "no MAC provided and bluetooth.primary_mac is empty"

connect_mac "$MAC"
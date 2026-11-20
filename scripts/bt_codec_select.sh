#!/usr/bin/env bash
# Switch a connected A2DP device to the best (or a requested) codec.
#
# Uses bluealsa-cli (source-built bluez-alsa >= 4.x, see build_bluealsa.sh).
# Requires BlueZ "Experimental = true" so remote Stream End-Points are
# visible — without it only the currently negotiated codec is listed and
# this script is a no-op. Exits 0 when switching is unavailable so the
# connect flow never breaks on a stock (SBC-only) install.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

UPDATE_STATUS=true

usage() {
    cat <<'EOF'
Usage: bt_codec_select.sh [--no-status] [MAC] [CODEC]

Without CODEC, picks the best codec supported by both sides:
LDAC > aptX-HD > aptX > AAC > SBC. Updates bt_status.json afterwards
(--no-status skips that, for callers that refresh status themselves).

Codecs that made a device stall (no AVDTP reply, link drop) are recorded
in /var/lib/audio-player/codec_denylist and skipped in auto mode.
Requesting a CODEC explicitly always tries it; success clears the entry.

SBC runs as SBC-XQ (dual channel) unless the device is denylisted as
"SBC-XQ"; the mode is set before connecting, so changing it reconnects.
CODEC=SBC-XQ clears that entry and reconnects on SBC-XQ.

  --deny-xq MAC   record that MAC can't sustain SBC-XQ and reconnect it on
                  standard SBC (used by the backend's xq_probe)
EOF
}

# SBC-XQ vs standard SBC: decided at negotiation by bt_prepare_bluealsa
# (_common.sh), never by a live switch — both speakers tested dropped the link
# on one. Changing a device's choice therefore means reconnecting it.
reconnect_for_sbc_quality() {
    local mac="$1"
    bt_device_connected "$mac" || return 0
    log "reconnecting ${mac} for SBC $(bt_sbc_quality_for "$mac")"
    bluetoothctl disconnect "$mac" >/dev/null 2>&1 || true
    local i
    for i in {1..10}; do
        bt_device_connected "$mac" || break
        sleep 0.5
    done
    bt_connect_session "$mac" || warn "reconnect failed for ${mac} — use Connect in the web UI"
    bt_wait_a2dp_pcm "$mac" 15 || warn "A2DP transport not back yet for ${mac}"
    update_status "$mac"
}

# The probe's verdict: remember it, then reconnect on standard SBC.
deny_xq() {
    local mac
    mac="$(normalize_mac "$1")" || die "invalid MAC: $1"
    deny_codec "$mac" "SBC-XQ"
    reconnect_for_sbc_quality "$mac"
}

# The UI's "Retry XQ": forget the verdict and reconnect on SBC-XQ.
retry_xq() {
    local mac
    mac="$(normalize_mac "$1")" || die "invalid MAC: $1"
    allow_codec "$mac" "SBC-XQ"
    reconnect_for_sbc_quality "$mac"
}

deny_codec() {
    local mac="$1" codec="$2"
    bt_codec_denied "$mac" "$codec" && return 0
    mkdir -p "$STATE_DIR"
    echo "${mac} ${codec}" >> "$BT_CODEC_DENYLIST"
    warn "denylisted ${codec} for ${mac} — device stalls on this codec (edit ${BT_CODEC_DENYLIST} to retry)"
}

allow_codec() {
    local mac="$1" codec="$2"
    [[ -f "$BT_CODEC_DENYLIST" ]] || return 0
    grep -vixF "${mac} ${codec}" "$BT_CODEC_DENYLIST" > "${BT_CODEC_DENYLIST}.tmp" || true
    mv "${BT_CODEC_DENYLIST}.tmp" "$BT_CODEC_DENYLIST"
}

update_status() {
    $UPDATE_STATUS || return 0
    "${SCRIPT_DIR}/bt_codec.sh" --update-status "$1" >/dev/null || true
}

# A failed AVDTP reconfigure usually kills the transport (and sometimes the
# whole link). Restore a working SBC connection so audio keeps playing.
recover_connection() {
    local mac="$1"
    if bt_wait_a2dp_pcm "$mac" 3; then
        return 0
    fi
    warn "playback transport lost — reconnecting ${mac}"
    bluetoothctl disconnect "$mac" >/dev/null 2>&1 || true
    sleep 2
    timeout 30 bluetoothctl connect "$mac" >/dev/null 2>&1 || true
    if bt_wait_a2dp_pcm "$mac" 15; then
        log "connection recovered"
        return 0
    fi
    warn "could not restore A2DP transport — power-cycle the device or use the web UI Reconnect"
    return 1
}

main() {
    local mac="${1:-$(primary_mac || true)}"
    local want="${2:-}"
    if [[ "${want^^}" == "SBC-XQ" ]]; then
        retry_xq "$mac"
        bt_device_connected "$mac" || exit 1
        exit 0
    fi
    [[ -n "$mac" ]] || die "no MAC and bluetooth.primary_mac is empty"
    # Not $1: it is unset when the MAC came from primary_mac, and under set -u
    # naming it turned the message into "unbound variable".
    mac="$(normalize_mac "$mac")" || die "invalid MAC: ${mac}"

    if ! command -v bluealsa-cli >/dev/null 2>&1; then
        log "bluealsa-cli not installed — codec switching unavailable (run build_bluealsa.sh)"
        exit 0
    fi
    bt_device_connected "$mac" || die "device not connected: ${mac}"

    local pcm=""
    local i=0
    while [[ -z "$pcm" && $i -lt 10 ]]; do
        pcm="$(bt_a2dp_pcm_path "$mac")"
        [[ -n "$pcm" ]] || { sleep 1; i=$((i + 1)); }
    done
    [[ -n "$pcm" ]] || die "no a2dp-source PCM for ${mac} (bluealsa transport missing)"

    local codec_out current available
    codec_out="$(bluealsa-cli codec "$pcm" 2>/dev/null || true)"
    current="$(echo "$codec_out" | awk -F': *' '/^Selected codec:/{print $2; exit}')"
    available="$(echo "$codec_out" | awk -F': *' '/^Available codecs:/{print $2; exit}')"
    [[ -n "$current" ]] || die "cannot read current codec for ${pcm}"
    log "Current codec: ${current} — available: ${available:-?}"

    local target="$want"
    if [[ -z "$target" ]]; then
        local best="" rank=0 r c
        for c in $available; do
            bt_codec_denied "$mac" "$c" && continue
            r="$(codec_rank "$c")"
            if (( r > rank )); then rank=$r; best="$c"; fi
        done
        target="$best"
    fi
    [[ -n "$target" ]] || die "no usable codec found (available: ${available:-none})"

    if [[ "$target" == "$current" ]]; then
        log "Already using ${current} — nothing to do"
    # Whole tokens: grep -w counts "-" as a word break, so "aptX" was "offered"
    # by a device that only lists aptX-HD, and failed later as a reconfigure.
    elif [[ " ${available} " != *" ${target} "* ]]; then
        die "codec ${target} not offered by this device (available: ${available})"
    else
        log "Switching ${mac}: ${current} -> ${target}"
        local switched=true
        bluealsa-cli codec "$pcm" "$target" || switched=false
        if $switched; then
            sleep 2
            # `|| true`: if the device drops the link right after accepting
            # the switch, a failed re-read must not kill the script before
            # deny_codec/recover_connection run (set -e + pipefail).
            current="$(bluealsa-cli codec "$pcm" 2>/dev/null \
                | awk -F': *' '/^Selected codec:/{print $2; exit}' || true)"
            [[ "$current" == "$target" ]] || switched=false
        fi
        if ! $switched; then
            warn "codec switch to ${target} failed (device did not accept reconfiguration)"
            if [[ -z "$want" ]]; then
                deny_codec "$mac" "$target"
            fi
            recover_connection "$mac" || true
            update_status "$mac"
            # Auto mode stays quiet so the connect flow continues on the old
            # codec; an explicit request must surface the failure.
            [[ -z "$want" ]] && exit 0
            exit 1
        fi
        log "Codec switched to ${current}"
        [[ -n "$want" ]] && allow_codec "$mac" "$target"
    fi

    update_status "$mac"
}

POSITIONAL=()
DENY_XQ=false
for arg in "$@"; do
    case "$arg" in
        -h|--help) usage; exit 0 ;;
        --no-status) UPDATE_STATUS=false ;;
        --deny-xq) DENY_XQ=true ;;
        *) POSITIONAL+=("$arg") ;;
    esac
done
[[ "${EUID}" -eq 0 ]] || die "run as root: sudo $0 ..."
if $DENY_XQ; then
    [[ ${#POSITIONAL[@]} -ge 1 ]] || die "--deny-xq needs a MAC"
    deny_xq "${POSITIONAL[0]}"
    exit 0
fi
main "${POSITIONAL[@]}"

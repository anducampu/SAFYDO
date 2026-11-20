#!/usr/bin/env bash
# Detect negotiated Bluetooth A2DP codec and write JSON status
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

UPDATE_STATUS=false
MAC=""

usage() {
    cat <<'EOF'
Usage: bt_codec.sh [MAC] [--update-status]

Print JSON with connection + codec info. With --update-status, writes
/var/lib/audio-player/bt_status.json
EOF
}

# Map bluealsa / dbus codec ids to human names
codec_name_from_id() {
    case "$1" in
        0|SBC|sbc) echo "SBC" ;;
        1|MPEG12|MPEG2|MPEG) echo "MPEG" ;;
        2|AAC|aac) echo "AAC" ;;
        3|aptX|APTX) echo "aptX" ;;
        4|aptX-HD|APTX_HD) echo "aptX-HD" ;;
        5|LDAC|ldac) echo "LDAC" ;;
        *) echo "$1" ;;
    esac
}

detect_codec_bluealsa_cli() {
    local mac="$1"
    command -v bluealsa-cli >/dev/null 2>&1 || return 1

    local pcm_path
    pcm_path="$(bt_a2dp_pcm_path "$mac")"
    [[ -n "$pcm_path" ]] || return 1

    local info
    info="$(bluealsa-cli -v info "$pcm_path" 2>/dev/null || true)"
    local codec cfg rate channels
    # bluez-alsa >= 4.x prints "Selected codec:", older builds "Codec:".
    # With -v the codec carries its config blob: "Selected codec: SBC:2415022e"
    codec="$(echo "$info" | sed -nE 's/^(Selected )?[Cc]odec: *//p' | head -1)"
    cfg="${codec#*:}"
    codec="${codec%%:*}"
    [[ "$cfg" == "$codec" ]] && cfg=""
    rate="$(echo "$info" | awk -F': *' '/Sampling:/{print $2; exit}' | grep -oE '[0-9]+' | head -1 || true)"
    channels="$(echo "$info" | awk -F': *' '/Channels:/{print $2; exit}' | grep -oE '[0-9]+' | head -1 || true)"

    [[ -n "$codec" ]] || return 1
    codec="$(codec_name_from_id "$codec")"
    # SBC config byte 0 low nibble: 8=mono 4=dual 2=stereo 1=joint.
    # Dual channel = the XQ high-bitrate mode (~2x standard SBC).
    if [[ "$codec" == "SBC" && "$cfg" =~ ^[0-9a-fA-F]{8}$ ]]; then
        local mode=$(( 0x${cfg:0:2} & 0x0f ))
        [[ "$mode" -eq 4 ]] && codec="SBC-XQ"
    fi
    echo "${codec}|${rate:-0}|${channels:-2}"
}

detect_codec_dbus() {
    local mac="$1"
    local path="/org/bluealsa/hci0/dev_${mac//:/_}"
    command -v dbus-send >/dev/null 2>&1 || return 1

    local reply codec
    reply="$(dbus-send --system --dest=org.bluealsa --print-reply \
        "$path" org.freedesktop.DBus.Properties.Get \
        string:org.bluealsa.PCM1 string:Codec 2>/dev/null || true)"
    codec="$(echo "$reply" | awk '/variant/ {print $3; exit}')"
    [[ -n "$codec" && "$codec" != *"Error"* ]] || return 1
    codec="$(codec_name_from_id "$codec")"
    echo "${codec}|0|2"
}

detect_codec_journal() {
    local mac="$1"
    local mac_lc="${mac,,}"
    local line codec rate=48000 channels=2
    line="$(journalctl -u bluealsa --no-pager -n 200 2>/dev/null | grep -iE "A2DP Source \(|SBC setup:" | tail -1 || true)"
    [[ -n "$line" ]] || return 1
    if echo "$line" | grep -qi 'LDAC'; then codec="LDAC"
    elif echo "$line" | grep -qi 'AAC'; then codec="AAC"
    elif echo "$line" | grep -qi 'aptX-HD'; then codec="aptX-HD"
    elif echo "$line" | grep -qi 'aptX'; then codec="aptX"
    else codec="SBC"
    fi
    if echo "$line" | grep -q "SBC setup:"; then
        rate="$(echo "$line" | sed -n 's/.* \([0-9]*\) Hz .*/\1/p')"
        [[ -n "$rate" ]] || rate=48000
    fi
    echo "${codec}|${rate}|${channels}"
}

# Only for the codec actually negotiated: the journal is not per device, and
# reading the last "SBC setup:" line whatever the codec reported an AAC or aptX
# link at the bitrate of some earlier SBC session.
detect_bitrate_from_journal() {
    local mac="$1" codec="${2:-}"
    local line bps pattern
    case "$codec" in
        SBC|SBC-XQ) pattern="SBC setup:" ;;
        LDAC)       pattern="LDAC" ;;
        *)          return 1 ;;
    esac
    line="$(journalctl -u bluealsa --no-pager -n 200 2>/dev/null | grep -i -- "$pattern" | tail -1 || true)"
    [[ -n "$line" ]] || return 1
    bps="$(echo "$line" | sed -n 's/.*=> \([0-9]*\) bps.*/\1/p')"
    [[ -n "$bps" && "$bps" -gt 0 ]] || return 1
    echo $((bps / 1000))
}

device_supported_codecs() {
    local mac="$1"
    local mac_lc="${mac,,}"
    # Preferred: live list from bluealsa-cli (needs BlueZ Experimental for
    # remote SEPs). Falls back to bluealsa journal endpoint messages.
    if command -v bluealsa-cli >/dev/null 2>&1; then
        local pcm avail
        pcm="$(bt_a2dp_pcm_path "$mac")"
        if [[ -n "$pcm" ]]; then
            avail="$(bluealsa-cli codec "$pcm" 2>/dev/null \
                | awk -F': *' '/^Available codecs:/{print $2; exit}' \
                | tr ' ' ',' | sed 's/,\+/,/g;s/^,//;s/,$//' || true)"
            if [[ -n "$avail" ]]; then
                echo "$avail"
                return 0
            fi
        fi
    fi
    journalctl -u bluealsa --no-pager -n 300 2>/dev/null \
        | grep -i "Adding new Stream End-Point: ${mac_lc}" \
        | sed -n 's/.*: \([A-Za-z0-9-]*\)$/\1/p' \
        | sort -u \
        | tr '\n' ',' \
        | sed 's/,$//'
}

pi_source_codecs() {
    if command -v bluealsa >/dev/null 2>&1; then
        bluealsa --help 2>&1 \
            | awk '/a2dp-source:/{sub(/.*a2dp-source:/,""); gsub(/[[:space:]]+/,""); print; exit}' \
            | tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | grep -v '^$' \
            | sort -u | tr '\n' ',' | sed 's/,$//'
        return 0
    fi
    echo "SBC,LDAC"
}

best_common_codec() {
    local device_csv="$1" pi_csv="$2"
    local best="" rank=0 r d p
    IFS=',' read -ra dev <<< "$device_csv"
    IFS=',' read -ra pi <<< "$pi_csv"
    for d in "${dev[@]}"; do
        [[ -n "$d" ]] || continue
        for p in "${pi[@]}"; do
            [[ "$d" == "$p" ]] || continue
            r="$(codec_rank "$d")"
            if (( r > rank )); then
                rank=$r
                best="$d"
            fi
        done
    done
    echo "$best"
}

estimate_bitrate_kbps() {
    local codec="$1"
    case "$codec" in
        SBC) echo 328 ;;
        SBC-XQ) echo 570 ;;
        AAC) echo 320 ;;
        aptX) echo 352 ;;
        aptX-HD) echo 576 ;;
        LDAC) echo 990 ;;
        *) echo 0 ;;
    esac
}

# Drop codecs this device stalls on (recorded by bt_codec_select.sh) so
# best_codec reflects what is actually usable, not what is advertised.
filter_denied_codecs() {
    local mac="$1" csv="$2"
    [[ -f "$BT_CODEC_DENYLIST" ]] || { echo "$csv"; return 0; }
    local out="" c
    IFS=',' read -ra arr <<< "$csv"
    for c in "${arr[@]}"; do
        [[ -n "$c" ]] || continue
        bt_codec_denied "$mac" "$c" && continue
        out+="${c},"
    done
    echo "${out%,}"
}

build_status_json() {
    local mac="$1"
    local connected=false
    local name=""
    local codec="unknown"
    local sample_rate=0
    local channels=2
    local bitrate_kbps=0

    if bt_device_connected "$mac"; then
        connected=true
        name="$(bt_device_name "$mac")"
        local detected=""
        detected="$(detect_codec_bluealsa_cli "$mac" || detect_codec_dbus "$mac" || detect_codec_journal "$mac" || true)"
        if [[ -n "$detected" ]]; then
            IFS='|' read -r codec sample_rate channels <<< "$detected"
        else
            codec="SBC"
            warn "codec detection fallback to SBC — start playback to refresh"
        fi
        local live_kbps=""
        live_kbps="$(detect_bitrate_from_journal "$mac" "$codec" || true)"
        if [[ -n "$live_kbps" ]]; then
            bitrate_kbps="$live_kbps"
        else
            bitrate_kbps="$(estimate_bitrate_kbps "$codec")"
        fi
    fi

    local device_codecs pi_codecs usable_codecs best_codec is_best="false"
    device_codecs="$(device_supported_codecs "$mac")"
    pi_codecs="$(pi_source_codecs)"
    [[ -z "$device_codecs" ]] && device_codecs="SBC"
    usable_codecs="$(filter_denied_codecs "$mac" "$device_codecs")"
    [[ -z "$usable_codecs" ]] && usable_codecs="SBC"
    best_codec="$(best_common_codec "$usable_codecs" "$pi_codecs")"
    [[ -z "$best_codec" ]] && best_codec="SBC"
    # "SBC-XQ" is still SBC at the negotiation level
    if [[ "${codec%-XQ}" == "$best_codec" ]]; then
        is_best="true"
    fi

    jq -n \
        --argjson connected "$connected" \
        --arg mac "$mac" \
        --arg name "$name" \
        --arg codec "$codec" \
        --argjson sample_rate "${sample_rate:-0}" \
        --argjson channels "${channels:-2}" \
        --argjson bitrate_kbps "$bitrate_kbps" \
        --arg device_codecs "$device_codecs" \
        --arg pi_codecs "$pi_codecs" \
        --arg best_codec "$best_codec" \
        --argjson is_best_quality "$is_best" \
        --argjson xq_denied "$(bt_codec_denied "$mac" "SBC-XQ" && echo true || echo false)" \
        --arg audio_device "$(bt_mpv_audio_device "$mac")" \
        --arg updated_at "$(date -Iseconds)" \
        '{
            connected: $connected,
            device_mac: $mac,
            device_name: $name,
            codec: $codec,
            sample_rate: (if $sample_rate == 0 then null else $sample_rate end),
            channels: $channels,
            bitrate_kbps: (if $bitrate_kbps == 0 then null else $bitrate_kbps end),
            device_codecs: ($device_codecs | split(",") | map(select(length > 0))),
            pi_codecs: ($pi_codecs | split(",") | map(select(length > 0))),
            best_codec: $best_codec,
            is_best_quality: $is_best_quality,
            xq_denied: $xq_denied,
            audio_device: $audio_device,
            updated_at: $updated_at
        }'
}

main() {
    require_cmd bluetoothctl jq python3

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --update-status) UPDATE_STATUS=true; shift ;;
            -h|--help) usage; exit 0 ;;
            *)
                MAC="$(normalize_mac "$1")" || die "invalid MAC: $1"
                shift
                ;;
        esac
    done

    MAC="${MAC:-$(primary_mac || true)}"
    [[ -n "$MAC" ]] || die "no MAC and bluetooth.primary_mac is empty"

    bt_ensure_ready
    local json
    json="$(build_status_json "$MAC")"
    echo "$json"
    if $UPDATE_STATUS; then
        write_bt_status "$json"
        log "Status written to ${BT_STATUS_FILE}"
        # Covers device-initiated reconnects the connect scripts never see
        # (reconciler calls --update-status); deduped inside the helper.
        if [[ "$(echo "$json" | jq -r '.connected')" == "true" ]]; then
            bt_mark_connected "$MAC" "$(echo "$json" | jq -r '.device_name // ""')" || true
        fi
    fi
}

[[ "${EUID}" -eq 0 ]] || die "run as root: sudo $0 ..."
main "$@"
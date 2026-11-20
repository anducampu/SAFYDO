#!/usr/bin/env bash
# Open a brief A2DP stream after BT connect so BlueALSA negotiates the best codec.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

MAC=""
PRIME_SECS="${PRIME_SECS:-0.4}"
TEST_WAV="/usr/share/sounds/alsa/Front_Center.wav"

usage() {
    cat <<'EOF'
Usage: bt_codec_prime.sh [MAC]

After Bluetooth connect, play a short sample to force A2DP codec negotiation
(LDAC preferred over SBC when both sides support it). Updates bt_status.json.
EOF
}

codec_prime_enabled() {
    local flag
    flag="$(cfg_get bluetooth.codec_prime_on_connect 2>/dev/null || echo true)"
    flag="${flag,,}"
    [[ "$flag" == "true" || "$flag" == "1" || "$flag" == "yes" ]]
}

# NOTE: older versions grepped the journal for "Adding new Stream End-Point"
# — bluez-alsa 4.3.x never logs that at default level, so every connect
# burned 15 s in a dead wait. Poll the actual PCM instead.

prime_a2dp_stream() {
    local mac="$1"
    local audio_dev pcm volume

    audio_dev="$(bt_mpv_audio_device "$mac")"
    pcm="$(bt_bluealsa_pcm "$mac")"
    volume="$(cfg_get audio.volume 2>/dev/null || echo 80)"

    if command -v mpv >/dev/null 2>&1 && [[ -f "$TEST_WAV" ]]; then
        log "Priming A2DP (${PRIME_SECS}s) via mpv → ${audio_dev}"
        timeout 6 mpv --no-video --really-quiet \
            --length="$PRIME_SECS" \
            --volume="$volume" \
            --audio-device="$audio_dev" \
            "$TEST_WAV" 2>/dev/null || true
        return 0
    fi

    if command -v aplay >/dev/null 2>&1 && [[ -f "$TEST_WAV" ]]; then
        log "Priming A2DP via aplay → ${pcm}"
        timeout 4 aplay -D "$pcm" -q "$TEST_WAV" 2>/dev/null || true
        return 0
    fi

    warn "mpv/aplay or test WAV missing — codec prime skipped"
    return 1
}

main() {
    MAC="${1:-$(primary_mac || true)}"
    [[ -n "$MAC" ]] || die "no MAC and bluetooth.primary_mac is empty"
    MAC="$(normalize_mac "$MAC")" || die "invalid MAC: ${MAC}"

    if ! codec_prime_enabled; then
        log "codec_prime_on_connect disabled — status refresh only"
        "${SCRIPT_DIR}/bt_codec.sh" --update-status "$MAC" || true
        exit 0
    fi

    if ! bt_device_connected "$MAC"; then
        die "device not connected: ${MAC}"
    fi

    bt_ensure_ready
    bt_wait_a2dp_pcm "$MAC" 15 || warn "A2DP playback PCM not up yet"

    # Pick the best codec both sides support before opening the stream
    # (--no-status: we refresh bt_status.json ourselves after priming)
    "${SCRIPT_DIR}/bt_codec_select.sh" --no-status "$MAC" || warn "codec auto-select failed — keeping negotiated codec"

    prime_a2dp_stream "$MAC"
    sleep 0.3

    local json codec="?" best="?" is_best=false
    json="$("${SCRIPT_DIR}/bt_codec.sh" --update-status "$MAC")"
    # One jq pass (bt_codec.sh already requires jq) instead of three python3
    # starts, each ~0.3 s on a Zero 2W, on every connect.
    IFS=$'\t' read -r codec best is_best < <(jq -r \
        '[.codec // "?", .best_codec // "?", (.is_best_quality // false | tostring)] | @tsv' \
        <<<"$json" 2>/dev/null) || true
    codec="${codec:-?}" best="${best:-?}"

    if [[ "$is_best" == "true" ]]; then
        log "Codec negotiated: ${codec} (best available for this device)"
    else
        warn "Codec negotiated: ${codec} — device best: ${best}"
    fi
}

[[ "${EUID}" -eq 0 ]] || die "run as root: sudo $0 ..."
[[ "${1:-}" != "-h" && "${1:-}" != "--help" ]] || { usage; exit 0; }
main "$@"
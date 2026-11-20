#!/usr/bin/env bash
# mpv idle daemon with JSON IPC for Phase 2+ control
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

require_cmd mpv

MAC="$(primary_mac || true)"
[[ -n "$MAC" ]] || die "bluetooth.primary_mac not set — pair a device first"

# Wait for audio-player-bt-autoconnect (avoid racing bt_connect / killall bluetoothctl)
if ! bt_device_connected "$MAC"; then
    log "BT not connected — waiting for autoconnect (up to 120s)"
    wait=0
    while [[ $wait -lt 120 ]]; do
        if bt_device_connected "$MAC"; then
            break
        fi
        sleep 5
        wait=$((wait + 5))
    done
fi
bt_device_connected "$MAC" || die "BT not connected after 120s — power on speaker and retry"

AUDIO_DEV="$(bt_mpv_audio_device "$MAC")"
VOLUME="$(cfg_get audio.volume 2>/dev/null || echo 80)"
# Full level only for a speaker that has taken over the volume (the backend sets
# this after checking). Any other speaker gets the saved level, not a blast.
if [[ "$(cfg_get audio.volume_follow_hardware 2>/dev/null)" =~ ^([Tt]rue|1|yes)$ \
      && "$(cfg_get audio.speaker_volume_mac 2>/dev/null)" == "$MAC" ]]; then
    VOLUME=100
fi
SOCKET="${RUN_DIR}/mpv.sock"

mkdir -p "$RUN_DIR"
rm -f "$SOCKET"

log "Starting mpv IPC → ${AUDIO_DEV} (socket ${SOCKET})"

# Web links (YouTube, SoundCloud, ...) go through mpv's ytdl_hook. Pin the
# self-updating yt-dlp from install.sh's install_ytdlp(): the Debian package in
# /usr/bin falls behind YouTube within weeks. Audio only — --no-video still
# downloads a muxed video track, so ask yt-dlp for an audio format up front.
YTDL_OPTS=(--ytdl-format=bestaudio/best)
[[ -x /usr/local/bin/yt-dlp ]] && YTDL_OPTS+=(--script-opts=ytdl_hook-ytdl_path=/usr/local/bin/yt-dlp)

# Online-stream tuning (Settings -> Streams): cache, rebuffer, timeout, user
# agent, reconnect, output buffer. One source of truth with the backend, which
# applies the same values live. The fallback is the tuning this script had
# hard-coded, so a broken Python or config never stops mpv from starting.
mapfile -t STREAM_OPTS < <(cd "$INSTALL_DIR" && python3 -m backend.services.stream_settings flags 2>/dev/null)
if (( ${#STREAM_OPTS[@]} == 0 )); then
    STREAM_OPTS=(
        --network-timeout=30
        --demuxer-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=10
        --audio-buffer=0.5
        --cache-secs=60
    )
fi

# Why the defaults (backend/services/stream_settings.py): reconnect makes
#   ffmpeg re-open a dropped ICY/HTTP stream instead of erroring out (WiFi
#   shares the Zero 2W's single 2.4 GHz radio with A2DP, so brief stalls are
#   normal); audio-buffer 0.5 s absorbs scheduler/BT hiccups (0.2 s caused
#   "Audio device underrun detected" bursts on bluealsa).

# Allocator: jemalloc, when installed (MPV_JEMALLOC=0 opts out). mpv's demuxer
# cache is millions of ~0.4-1 KB packets on glibc's main heap, and glibc only
# gives memory back from the top of the heap. After a long or high-bitrate
# stream the freed cache stayed in mpv: 2026-11-20 on the 3 A+ the heap held
# 320 MB (296 MB of it in zram swap) while the live cache was 5 MB, leaving
# 42 MB available. A malloc_trim(0) through gdb dropped it to 56 MB of swap
# and 252 MB available, so it was free memory that was never returned.
# jemalloc purges unused pages after the decay time. narenas:2 keeps the
# per-arena overhead down on a 4-core 512 MB board; muzzy_decay 0 skips the
# MADV_FREE stage, so the kernel sees the memory as free immediately.
if [[ "${MPV_JEMALLOC:-1}" != "0" ]]; then
    JEMALLOC="$(PATH="${PATH}:/usr/sbin:/sbin" ldconfig -p 2>/dev/null | awk '/libjemalloc\.so\.2 /{print $NF; exit}')"
    if [[ -n "$JEMALLOC" && -r "$JEMALLOC" ]]; then
        export LD_PRELOAD="${JEMALLOC}${LD_PRELOAD:+:${LD_PRELOAD}}"
        export MALLOC_CONF="${MALLOC_CONF:-narenas:2,dirty_decay_ms:5000,muzzy_decay_ms:0}"
    fi
fi

# Headless trims (measured on the 3 A+ 2026-11-20, mpv 0.40):
#   --quiet  drops the terminal status line, which reached the journal at
#            ~800 lines/min (journald ~1 % CPU and steady churn of the in-RAM
#            journal). Warnings and errors (underruns, failed opens) still log.
#   built-in scripts that only serve a screen or keyboard: the OSC alone kept a
#            Lua thread at ~1.5 %; all of them together cost ~2 % CPU and 6 MB.
#            ytdl_hook stays (web links need it).
# Each flag is passed only if this mpv knows it: an unknown option stops mpv
# from starting, and Bookworm's 0.35 (Zero 2W) lacks the newer load-* ones.
MPV_OPTS_KNOWN="$(mpv --list-options 2>/dev/null | awk '{print $1}')"
HEADLESS_OPTS=()
for opt in --quiet --osc=no --load-stats-overlay=no --load-console=no \
           --load-select=no --load-commands=no --load-positioning=no \
           --load-auto-profiles=no; do
    grep -qxF -- "${opt%%=*}" <<<"$MPV_OPTS_KNOWN" && HEADLESS_OPTS+=("$opt")
done

# --demuxer-donate-buffer=no: mpv's default lets the back buffer borrow the
#   unused part of the 50 MB forward buffer. On a live stream the forward side
#   never fills, so the rewind window grew past the budget player.apply_rewind
#   sets (2026-11-20: 28 MB held = 34 min against an 8.2 MB / 10 min budget on a
#   48 kbps station), up to ~58 MB. Off, the back buffer is exactly the budget.
exec mpv \
    --no-video \
    --idle=yes \
    --keep-open=yes \
    --input-ipc-server="${SOCKET}" \
    --audio-device="${AUDIO_DEV}" \
    --volume="${VOLUME}" \
    --cache=yes \
    --demuxer-max-bytes=50M \
    --demuxer-donate-buffer=no \
    --audio-display=no \
    "${STREAM_OPTS[@]}" \
    "${HEADLESS_OPTS[@]}" \
    "${YTDL_OPTS[@]}"

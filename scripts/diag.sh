#!/usr/bin/env bash
# SAFYDO — diagnostic snapshot for debugging playback / Bluetooth / network
# drop-outs. Gathers the state and recent logs that matter when a stream stops
# or the BT speaker disconnects, into one timestamped file (tmpfs, no SD wear).
#
#   sudo /opt/rpi-audio-player/scripts/diag.sh          # write + print path
#   sudo /opt/rpi-audio-player/scripts/diag.sh --stdout # also dump to stdout
#
# The backend exposes this at GET /api/system/diag (used by the UI's
# "Diagnostics" button) so you can grab it without SSH.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh" 2>/dev/null || true
# _common.sh switches errexit on, which this script deliberately runs without:
# with it, one failing pipeline (json.tool on a corrupt reboot ledger — the very
# thing worth seeing) ended the report half way.
set +e

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="/run/audio-player/diag-${STAMP}.txt"
SINCE="${SINCE:--30 min}"
mkdir -p /run/audio-player 2>/dev/null || true

section() { printf '\n===== %s =====\n' "$1"; }

collect() {
    echo "SAFYDO diagnostics — ${STAMP}"
    echo "host: $(hostname)   uptime: $(uptime -p 2>/dev/null)   kernel: $(uname -r)"
    # Board × OS decides which install.sh branches ran (gadget, overclock,
    # package set) — the first thing to know when comparing two machines.
    if declare -F platform_describe >/dev/null 2>&1; then
        echo "platform: $(platform_describe)"
    fi

    section "Failed units"
    systemctl --failed --no-legend 2>/dev/null || true

    section "Player / audio services"
    for u in audio-player-backend audio-player-mpv bluealsa bluetooth \
             audio-player-bt-autoconnect audio-player-bt-retry.timer \
             audio-player-bt-watchdog.timer; do
        printf '%-34s %s\n' "$u" "$(systemctl is-active "$u" 2>/dev/null)"
    done

    section "mpv state (/api/status)"
    curl -s --max-time 4 http://127.0.0.1:8000/api/status 2>/dev/null \
        | python3 -m json.tool 2>/dev/null | sed -n '1,60p' || echo "(backend unreachable)"

    section "BT wedge watchdog"
    # It never fails its unit on purpose (a permanently failed unit would poison
    # the "zero failed units" check), so its verdict has to be read here.
    echo "now        : $(cat "${RUN_DIR:-/run/audio-player}/bt_watchdog.status" 2>/dev/null || echo '(no run yet)')"
    echo "last event : $(cat "${STATE_DIR:-/var/lib/audio-player}/bt_watchdog.status" 2>/dev/null || echo '(none)')"
    echo "rebind recovers this board: $(bt_rebind_verdict 2>/dev/null || echo '?')"
    echo "reboot escalation: $(cfg_get bluetooth.wedge_reboot 2>/dev/null || echo '?')"
    if [[ -s "${STATE_DIR:-/var/lib/audio-player}/bt_wedge_recovery.json" ]]; then
        echo "reboot ledger (24h):"
        python3 -m json.tool "${STATE_DIR:-/var/lib/audio-player}/bt_wedge_recovery.json" 2>/dev/null | sed -n '1,20p'
    fi
    # Names only: the logs bt_watchdog.sh saved before each wedge reboot (and at
    # a STUCK wedge left for a human to reboot). Read them with sudo.
    local evd="${STATE_DIR:-/var/lib/audio-player}/btwedge"
    if [[ -d "$evd" ]]; then
        echo "saved wedge logs (${evd}, newest first):"
        ls -lt "$evd" 2>/dev/null | grep ' wedge-' | sed -n '1,10p'
    fi

    section "Bluetooth adapter"
    bluetoothctl show 2>/dev/null | grep -iE "Name|Powered|Discoverable|Pairable|Alias" || true
    echo "primary_mac: $(cfg_get bluetooth.primary_mac 2>/dev/null || echo '?')"

    section "Paired BT devices"
    while read -r _ mac _; do
        [[ -n "${mac:-}" ]] || continue
        echo "--- $mac ---"
        bluetoothctl info "$mac" 2>/dev/null \
            | grep -iE "Name|Alias|Paired|Trusted|Connected|RSSI|Icon|Codec" || true
    done < <(bluetoothctl devices Paired 2>/dev/null)

    section "bluealsa PCMs (active codec/transport)"
    (bluealsa-cli list-pcms 2>/dev/null || /usr/bin/bluealsactl list-pcms 2>/dev/null) || echo "(bluealsa-cli unavailable)"

    section "RF kill / radios"
    rfkill list 2>/dev/null || /usr/sbin/rfkill list 2>/dev/null || true

    section "Network path"
    ip -4 route show default 2>/dev/null || true
    ip -br addr 2>/dev/null | grep -vE "^lo" || true

    section "Recent BT + audio kernel messages"
    dmesg 2>/dev/null | grep -iE "bluetooth|hci|firmware|usb .*disconnect|rtl|xhci|under-?run" | tail -n 25 \
        || echo "(dmesg needs root)"

    section "journal: bluetooth (${SINCE})"
    journalctl -u bluetooth --since "${SINCE}" --no-pager 2>/dev/null | tail -n 40 || echo "(needs root)"

    section "journal: bluealsa (${SINCE})"
    journalctl -u bluealsa --since "${SINCE}" --no-pager 2>/dev/null | tail -n 25 || true

    section "journal: mpv daemon (${SINCE})"
    journalctl -u audio-player-mpv --since "${SINCE}" --no-pager 2>/dev/null | tail -n 40 || true

    section "journal: backend (${SINCE})"
    journalctl -u audio-player-backend --since "${SINCE}" --no-pager 2>/dev/null \
        | grep -iE "error|warn|reconnect|bt|resume|traceback|exception" | tail -n 25 || true

    echo
    echo "===== end ====="
}

collect > "$OUT" 2>&1
[[ "${1:-}" == "--stdout" ]] && cat "$OUT"
echo "$OUT" >&2
echo "$OUT"

#!/usr/bin/env bash
# Raise the setup AP when the box has no way onto the network.
#
# Without this there is a chicken-and-egg: the captive portal is the tool for
# joining an unknown WiFi, but the only ways to start it were the web UI (which
# needs network) and SSH (which needs network, or a serial cable). Swap the
# ethernet dongle for a WiFi one in a place whose SSID the box has never seen
# and there is no way in at all.
#
# Deliberately conservative — an AP appearing by surprise is its own problem:
#   - opt-in, via network.captive_portal_enabled in player.yaml
#   - needs a wireless device (an AP needs a radio)
#   - needs GRACE_TICKS consecutive misses, so a slow DHCP lease or a dongle
#     still enumerating never triggers it
#   - never runs while a captive session is already up
#   - stands down by itself once a real uplink appears on another adapter
#     (only for an AP it raised itself)
#
# Usage: captive_watch.sh [--once]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

STATE_FILE="${RUN_DIR}/captive_watch.misses"
GRACE_TICKS="${GRACE_TICKS:-3}"

enabled() {
    local flag
    flag="$(cfg_get network.captive_portal_enabled 2>/dev/null || echo false)"
    [[ "${flag,,}" == "true" || "${flag}" == "1" ]]
}

reset_misses() { rm -f "$STATE_FILE" 2>/dev/null || true; }

bump_misses() {
    local n=0
    [[ -f "$STATE_FILE" ]] && n="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
    [[ "$n" =~ ^[0-9]+$ ]] || n=0
    n=$((n + 1))
    mkdir -p "$RUN_DIR" 2>/dev/null || true
    echo "$n" > "$STATE_FILE" 2>/dev/null || true
    echo "$n"
}

main() {
    enabled || { reset_misses; exit 0; }

    # Already serving the portal. One this watcher raised stands down by itself
    # once a real uplink is back (say an ethernet dongle is plugged in): exiting
    # here unconditionally left the AP up for good, the WiFi adapter unmanaged,
    # and the box off its saved network until someone stopped it by hand. One a
    # person started from the UI is theirs to stop. With a single radio the AP
    # cannot see the home network return — only a second adapter can end it.
    if [[ -e "${RUN_DIR}/captive/state" ]]; then
        if [[ -e "${RUN_DIR}/captive/auto" ]] && have_uplink; then
            log "uplink is back — taking down the setup AP this watcher raised"
            "${SCRIPT_DIR}/captive_portal.sh" stop || warn "captive portal stop failed"
            reset_misses
        fi
        exit 0
    fi

    if have_uplink; then
        reset_misses
        exit 0
    fi

    local wifi
    wifi="$(first_wireless_iface)"
    if [[ -z "$wifi" ]]; then
        # No radio to host an AP on. Say so once, then stay quiet: on a box
        # whose uplink is a wired dongle this is the normal state while it is
        # unplugged, and logging every tick would be noise.
        [[ -f "$STATE_FILE" ]] || log "no uplink and no wireless adapter — cannot raise a setup AP"
        bump_misses >/dev/null
        exit 0
    fi

    local misses
    misses="$(bump_misses)"
    if (( misses < GRACE_TICKS )); then
        log "no uplink (${misses}/${GRACE_TICKS}) — setup AP in ~$(( (GRACE_TICKS - misses) * 30 ))s if this persists"
        exit 0
    fi

    log "no uplink after ${misses} checks — raising the setup AP on ${wifi}"
    if AP_IF="$wifi" "${SCRIPT_DIR}/captive_portal.sh" start; then
        # Marks the AP as ours, so the stand-down above never touches one a
        # person started. Lives in the portal's run dir, which stop removes.
        : > "${RUN_DIR}/captive/auto" 2>/dev/null || true
        reset_misses
    else
        warn "captive portal failed to start — will retry on the next tick"
    fi
}

[[ "${EUID}" -eq 0 ]] || die "run as root: sudo $0"
main "$@"

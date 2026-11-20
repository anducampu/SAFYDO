#!/usr/bin/env bash
# React to a USB network adapter being plugged in or pulled out.
#
# The point is that swapping adapters is a normal thing to do on these boxes —
# the onboard WiFi is off so BT gets the whole 2.4 GHz radio, which means the
# uplink is always a dongle, and you may want ethernet today and WiFi tomorrow.
#
#   wifi dongle in   -> make sure NetworkManager's radio is on, so a saved SSID
#                       can actually be joined. network_ready.sh does this too,
#                       but only at boot, so a hot-plug alone left the radio off.
#   wifi dongle out  -> if no wireless device is left, switch the radio off
#                       rather than leave NM scanning for hardware that is gone.
#   any change       -> re-run the ethernet-priority pass so metrics match the
#                       adapters actually present.
#
# Invoked per-interface by udev via audio-player-net-hotplug@.service.
#
# Usage: net_hotplug.sh add|remove <iface>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

ACTION="${1:-}"
IFACE="${2:-}"

[[ -n "$ACTION" && -n "$IFACE" ]] || die "usage: $0 add|remove <iface>"

# A captive AP owns the radio deliberately; never fight it.
if [[ -e "${RUN_DIR}/captive/state" ]]; then
    log "captive portal active — leaving the radio alone (${ACTION} ${IFACE})"
    exit 0
fi

radio_on() {
    command -v nmcli >/dev/null 2>&1 || return 0
    [[ "$(nmcli radio wifi 2>/dev/null)" == "enabled" ]] && return 0
    log "enabling WiFi radio (wireless adapter present)"
    nmcli radio wifi on 2>/dev/null || true
}

radio_off() {
    command -v nmcli >/dev/null 2>&1 || return 0
    [[ "$(nmcli radio wifi 2>/dev/null)" == "disabled" ]] && return 0
    log "no wireless adapter left — switching the WiFi radio off"
    nmcli radio wifi off 2>/dev/null || true
}

case "$ACTION" in
    add)
        # Trust the hardware, not the name: a dongle may be wlan0 or wlx<mac>.
        if [[ -d "/sys/class/net/${IFACE}/wireless" ]]; then
            log "wireless adapter ${IFACE} appeared"
            radio_on
            nmcli dev set "$IFACE" managed yes 2>/dev/null || true
        else
            log "wired adapter ${IFACE} appeared"
        fi
        ;;
    remove)
        # The sysfs entry is already gone by now, so decide on what remains.
        if [[ -z "$(first_wireless_iface)" ]]; then
            radio_off
        fi
        log "adapter ${IFACE} removed"
        ;;
    *)
        die "unknown action: ${ACTION}"
        ;;
esac

# Re-evaluate ethernet-vs-WiFi metrics for the adapters now present. Best
# effort: a hotplug must never fail because priority tuning is disabled.
if [[ -x "${SCRIPT_DIR}/network_priority.sh" ]]; then
    "${SCRIPT_DIR}/network_priority.sh" >/dev/null 2>&1 || true
fi

exit 0

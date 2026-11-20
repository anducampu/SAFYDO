#!/usr/bin/env bash
# Ensure some network path is up after boot (Bookworm + NetworkManager).
# Works with onboard WiFi (wlan0), USB WiFi dongles (wlx*), and USB ethernet
# dongles (enx*/eth*) — NM autoconnects them; this remediates WiFi races and
# verifies connectivity. The USB gadget dev link (usb0) never has a default
# route, so it does not count as "online".
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

MAX_WAIT="${MAX_WAIT:-90}"

captive_active() {
    [[ -f /run/audio-player/captive/state ]]
}

have_default_route() {
    ip -4 route show default 2>/dev/null | grep -q .
}

wlan_has_ipv4() {
    [[ -n "${WLAN_IF:-}" ]] || return 1
    ip -4 addr show "$WLAN_IF" 2>/dev/null | grep -qE 'inet [0-9]'
}

wait_connectivity() {
    local i=0
    while (( i < MAX_WAIT )); do
        have_default_route && return 0
        nmcli networking connectivity check 2>/dev/null | grep -q full && return 0
        wlan_has_ipv4 && return 0
        sleep 2
        i=$((i + 2))
    done
    return 1
}

bring_up_wifi() {
    require_cmd nmcli ip

    # Only power the radio when a wireless device is actually present, so boot
    # and hotplug agree: net_hotplug.sh switches it off when the last adapter
    # leaves, and this used to switch it straight back on at the next boot.
    local wifi_if
    wifi_if="$(first_wireless_iface)"
    if [[ -z "$wifi_if" ]]; then
        # Assert it rather than assume it, so two boards with different
        # histories reach the same state: no adapter means radio off, whether
        # the last one was unplugged or was never there.
        if [[ "$(nmcli radio wifi 2>/dev/null)" == "enabled" ]]; then
            nmcli radio wifi off 2>/dev/null || true
        fi
        log "no wireless adapter — WiFi radio off"
        return 1
    fi
    WLAN_IF="${WLAN_IF:-$wifi_if}"
    [[ -d "/sys/class/net/${WLAN_IF}/wireless" ]] || WLAN_IF="$wifi_if"

    rfkill unblock wifi 2>/dev/null || true
    nmcli radio wifi on 2>/dev/null || true
    nmcli dev set "$WLAN_IF" managed yes 2>/dev/null || true

    # Prefer autoconnect WiFi profile(s)
    local con
    for con in $(nmcli -t -f NAME,TYPE con show --active 2>/dev/null | awk -F: '$2=="802-11-wireless"{print $1}'); do
        [[ -n "$con" ]] || continue
        nmcli con up "$con" 2>/dev/null && return 0
    done

    for con in $(nmcli -t -f NAME,TYPE con show 2>/dev/null | awk -F: '$2=="802-11-wireless"{print $1}'); do
        [[ -n "$con" ]] || continue
        nmcli con up "$con" 2>/dev/null && return 0
    done

    nmcli dev connect "$WLAN_IF" 2>/dev/null || true
}

main() {
    if captive_active; then
        log "Captive portal active — skip network_ready"
        exit 0
    fi

    if ! command -v nmcli >/dev/null 2>&1; then
        warn "nmcli missing — skip network_ready"
        exit 0
    fi

    # Assert the radio state before the early exit, so it is deterministic on a
    # box that is already online. Both halves have to be asserted, not just one:
    # `nmcli radio wifi off` persists, and NetworkManager re-applies it (as an
    # rfkill soft block) to a radio that only appears later. Asserting the "off"
    # half alone therefore left a box that is online over ethernet with its WiFi
    # switched off and an adapter sitting right there, because the matching "on"
    # lives in bring_up_wifi() — below the have_default_route early exit, so it
    # never ran. Seen for real on the 3 A+: phy0 soft-blocked at every boot.
    if [[ -e "${RUN_DIR}/captive/state" ]]; then
        : # a captive AP owns the radio on purpose — never fight it
    elif [[ -n "$(first_wireless_iface)" ]]; then
        if [[ "$(nmcli radio wifi 2>/dev/null)" == "disabled" ]]; then
            log "wireless adapter present — switching the WiFi radio on"
            rfkill unblock wifi 2>/dev/null || true
            nmcli radio wifi on 2>/dev/null || true
        fi
    elif [[ "$(nmcli radio wifi 2>/dev/null)" == "enabled" ]]; then
        log "no wireless adapter — switching the WiFi radio off"
        nmcli radio wifi off 2>/dev/null || true
    fi

    if have_default_route; then
        log "Network already up: $(ip -4 route show default | head -n1)"
        exit 0
    fi

    # first_wireless_iface (_common.sh): wlan0 onboard or wlx* on a dongle.
    WLAN_IF="${WLAN_IF:-$(first_wireless_iface)}"
    if [[ -z "${WLAN_IF}" ]]; then
        # Onboard WiFi disabled and no WiFi dongle — ethernet dongle is NM's
        # job; nothing to remediate here. Don't fail boot over a missing cable.
        warn "no WiFi device and no default route — plug a network dongle or check the cable"
        exit 0
    fi

    log "No connectivity after boot — bringing up ${WLAN_IF} via NetworkManager"
    bring_up_wifi
    wait_connectivity || warn "network may still be down after ${MAX_WAIT}s"

    if have_default_route || wlan_has_ipv4; then
        log "Network ready: $(ip -4 route show default 2>/dev/null | head -n1)"
        exit 0
    fi

    warn "Not connected — check router / nmcli dev wifi list"
    exit 1
}

[[ "${EUID}" -eq 0 ]] || die "run as root: sudo $0 ..."
main "$@"

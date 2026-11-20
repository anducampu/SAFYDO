#!/usr/bin/env bash
# SAFYDO — diagnose USB network dongles (read-only).
# Shows what the kernel sees on USB, which drivers bound, what NetworkManager
# did with each interface, and where the default route points.
set -uo pipefail

section() { echo; echo "=== $1 ==="; }

section "USB devices"
lsusb 2>/dev/null || echo "(lsusb unavailable)"

section "Network interfaces"
ip -br link
echo
ip -br addr

section "Interface drivers"
for d in /sys/class/net/*; do
    iface="$(basename "$d")"
    [[ "$iface" == "lo" ]] && continue
    drv="$(readlink -f "$d/device/driver" 2>/dev/null | xargs -r basename)"
    wireless=""
    [[ -d "$d/wireless" ]] && wireless=" (wifi)"
    echo "${iface}: driver=${drv:-none}${wireless}"
done

section "NetworkManager devices"
nmcli device status 2>/dev/null || echo "(nmcli unavailable)"

section "Active connections"
nmcli -t -f NAME,TYPE,DEVICE connection show --active 2>/dev/null || true

section "Default route"
ip -4 route show default || echo "(no default route — no internet path)"

section "WiFi radios"
/usr/sbin/rfkill list 2>/dev/null || rfkill list 2>/dev/null || true

section "Recent USB/net kernel messages"
dmesg 2>/dev/null | grep -iE "usb [0-9]|usbcore|firmware|wlan|eth[0-9]|enx|wlx|renamed" | tail -n 20 \
    || echo "(dmesg unreadable — run with sudo)"

echo
echo "Hints:"
echo "  - Dongle in 'USB devices' but no new interface  -> kernel driver/firmware missing (check messages above)"
echo "  - Interface present but NM shows 'disconnected' -> ethernet: check cable; wifi: nmcli dev wifi list"
echo "  - WiFi dongles get MAC-based names (wlx...) — profiles are not pinned, autoconnect still works"

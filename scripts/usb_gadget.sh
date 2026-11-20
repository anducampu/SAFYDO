#!/usr/bin/env bash
# SAFYDO — USB composite gadget: serial console (ttyGS0) + ethernet (usb0).
#
# Dev/recovery link for when onboard WiFi is disabled. Tether the Pi's USB
# *data* port (inner micro-USB, labeled "USB") to a PC with a data cable:
#   - PC gets a serial port (/dev/ttyACM0) -> login console
#   - PC gets a USB ethernet iface -> DHCP 10.55.0.x -> ssh <user>@10.55.0.1
# With an OTG adapter the same port keeps acting as USB host (dwc2 dr_mode=otg
# switches roles by cable). Requires dtoverlay=dwc2,dr_mode=otg + reboot.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"  # log/warn/die

GADGET=/sys/kernel/config/usb_gadget/safydo
# Only covers the brief window before dwc2 registers the UDC at boot in
# peripheral mode; in host/dongle mode the UDC never appears (all wait wasted),
# so keep it short. Env-overridable.
UDC_WAIT_S="${UDC_WAIT_S:-4}"

cpu_serial() {
    local s
    s="$(awk '/^Serial/{print $NF}' /proc/cpuinfo 2>/dev/null | tail -n1)"
    [[ -n "$s" ]] || s="0000000000000000"
    echo "$s"
}

# Stable locally-administered MACs derived from the CPU serial (host=PC side,
# dev=Pi side) so the PC sees the same "adapter" across reboots.
mac_from_serial() {
    local nibble="$1" hex
    hex="$(cpu_serial)"
    hex="${hex: -6}"
    printf '02:%s:%s:%s:00:0%s\n' "${hex:0:2}" "${hex:2:2}" "${hex:4:2}" "$nibble"
}

# `|| true`: a missing /sys/class/udc (dwc2 not loaded / pre-reboot) makes `ls`
# exit non-zero, which pipefail would propagate and trip `set -e` in the callers'
# `udc="$(udc_name)"` assignments — aborting the graceful "no UDC" handling.
udc_name() { ls /sys/class/udc 2>/dev/null | head -n1 || true; }

up() {
    [[ "${EUID}" -eq 0 ]] || die "run as root"
    [[ -d /sys/kernel/config ]] || die "configfs not mounted"
    modprobe libcomposite 2>/dev/null || true

    local udc="" i=0
    while (( i < UDC_WAIT_S )); do
        udc="$(udc_name)"
        [[ -n "$udc" ]] && break
        sleep 1
        i=$((i + 1))
    done
    if [[ -z "$udc" ]]; then
        # No UDC means the port is in USB *host* mode (OTG adapter / dongle
        # attached) or dwc2 isn't loaded yet (pre-reboot). Host mode is a normal
        # running state, so skip gracefully instead of failing — a hard error
        # here would leave usb-gadget.service failed on every dongle boot.
        log "no UDC (USB host mode, or dwc2 overlay missing / pre-reboot) — skipping gadget"
        return 0
    fi

    if [[ -d "$GADGET" ]]; then
        if [[ -n "$(cat "$GADGET/UDC" 2>/dev/null)" ]]; then
            log "already bound to $(cat "$GADGET/UDC")"
            return 0
        fi
        # Present but unbound: reuse only if fully built. A prior aborted build
        # can leave a partial tree that would fail to bind and wedge every later
        # run — tear it down and rebuild from scratch instead.
        if [[ ! -e "$GADGET/configs/c.1/acm.GS0" || ! -e "$GADGET/configs/c.1/ecm.usb0" ]]; then
            log "incomplete gadget tree — rebuilding"
            down
        fi
    fi

    if [[ ! -d "$GADGET" ]]; then
        mkdir -p "$GADGET"
        echo 0x1d6b > "$GADGET/idVendor"    # Linux Foundation
        echo 0x0104 > "$GADGET/idProduct"   # Multifunction composite gadget
        echo 0x0100 > "$GADGET/bcdDevice"
        echo 0x0200 > "$GADGET/bcdUSB"

        mkdir -p "$GADGET/strings/0x409"
        cpu_serial > "$GADGET/strings/0x409/serialnumber"
        echo "SAFYDO" > "$GADGET/strings/0x409/manufacturer"
        echo "SAFYDO dev link (serial + ethernet)" > "$GADGET/strings/0x409/product"

        mkdir -p "$GADGET/configs/c.1/strings/0x409"
        echo "ACM console + ECM ethernet" > "$GADGET/configs/c.1/strings/0x409/configuration"
        echo 250 > "$GADGET/configs/c.1/MaxPower"

        mkdir -p "$GADGET/functions/acm.GS0"
        mkdir -p "$GADGET/functions/ecm.usb0"
        mac_from_serial 1 > "$GADGET/functions/ecm.usb0/host_addr"
        mac_from_serial 2 > "$GADGET/functions/ecm.usb0/dev_addr"

        ln -sf "$GADGET/functions/acm.GS0" "$GADGET/configs/c.1/"
        ln -sf "$GADGET/functions/ecm.usb0" "$GADGET/configs/c.1/"
    fi

    echo "$udc" > "$GADGET/UDC"
    log "bound to ${udc}: serial ttyGS0 + ethernet usb0"
}

down() {
    [[ "${EUID}" -eq 0 ]] || die "run as root"
    [[ -d "$GADGET" ]] || { log "gadget not present"; return 0; }
    echo "" > "$GADGET/UDC" 2>/dev/null || true
    rm -f "$GADGET/configs/c.1/acm.GS0" "$GADGET/configs/c.1/ecm.usb0"
    rmdir "$GADGET/configs/c.1/strings/0x409" "$GADGET/configs/c.1" \
          "$GADGET/functions/acm.GS0" "$GADGET/functions/ecm.usb0" \
          "$GADGET/strings/0x409" "$GADGET" 2>/dev/null || true
    log "gadget removed"
}

status() {
    local udc
    udc="$(udc_name)"
    echo "udc:     ${udc:-none (dwc2 overlay missing or not rebooted yet)}"
    echo "gadget:  $([[ -d "$GADGET" ]] && echo present || echo absent)"
    echo "bound:   $(cat "$GADGET/UDC" 2>/dev/null || echo no)"
    echo "usb0:    $(ip -br addr show usb0 2>/dev/null || echo missing)"
    echo "carrier: $(cat /sys/class/net/usb0/carrier 2>/dev/null || echo n/a)"
    echo "getty:   $(systemctl is-active serial-getty@ttyGS0.service 2>/dev/null || true)"
    echo "dhcp:    $(systemctl is-active usb-gadget-dhcp.service 2>/dev/null || true)"
}

case "${1:-}" in
    up)     up ;;
    down)   down ;;
    status) status ;;
    *)      echo "Usage: $0 up|down|status" >&2; exit 1 ;;
esac

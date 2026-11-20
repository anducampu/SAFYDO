#!/usr/bin/env bash
# SAFYDO — enable/disable the onboard WiFi radio (dtoverlay=disable-wifi).
#
# Why: the Zero 2W shares one 2.4 GHz radio between WiFi and BT — with WiFi
# off, A2DP gets the whole airtime. disable-wifi cuts only the WLAN half of
# the combo chip; onboard Bluetooth keeps working. The overlay is model-generic
# and works the same on the 3 A+ — note that board is dual-band, so parking
# WiFi on 5 GHz is a valid alternative to switching it off (leaves the single
# USB port free for storage).
#
# Network afterwards comes from a USB dongle (ethernet or WiFi — handled by
# NetworkManager) or, on the Zero 2W only, the USB dev link (usb_gadget.sh /
# docs/usb-dev-link.md).
#
# SD-card recovery if locked out: mount the boot partition on a PC and delete
# the block between the SAFYDO onboard-wifi markers in config.txt.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"  # log/warn/die + boot_config_path

BEGIN_MARK="# --- SAFYDO onboard-wifi-disable begin ---"
END_MARK="# --- SAFYDO onboard-wifi-disable end ---"

boot_cfg() {
    local cfg
    cfg="$(boot_config_path)"
    [[ -n "$cfg" ]] || die "config.txt not found"
    echo "$cfg"
}

block_present() { grep -qF "$BEGIN_MARK" "$(boot_cfg)"; }

status() {
    local cfg
    cfg="$(boot_cfg)"
    if block_present; then
        echo "config.txt: onboard WiFi DISABLED (takes effect at boot)"
    else
        echo "config.txt: onboard WiFi enabled"
    fi
    echo "wifi phys:  $(ls /sys/class/ieee80211 2>/dev/null | tr '\n' ' ')"
    echo "wlan ifs:   $(ls -d /sys/class/net/*/wireless 2>/dev/null | xargs -r -n1 dirname | xargs -r -n1 basename | tr '\n' ' ')"
    echo "default rt: $(ip -4 route show default 2>/dev/null | head -n1)"
    echo "board/os:   $(platform_board)/$(platform_os)"
    echo "usb uplink: $(usb_uplink_iface) (dongle with carrier + IP)"
    if board_supports_gadget; then
        echo "usb link:   $(systemctl is-active usb-gadget.service 2>/dev/null || true) ($(ip -br addr show usb0 2>/dev/null || echo usb0 missing))"
    else
        echo "usb link:   n/a on this board (GPIO serial console is the fallback)"
    fi
}

checklist() {
    if board_supports_gadget; then
        cat >&2 <<EOF
Refusing to disable onboard WiFi without --confirm. Verify first:
  1. USB dev link works: tether the Pi's USB data port to the PC, then
     'ssh <user>@10.55.0.1' or 'screen /dev/ttyACM0 115200' must succeed.
  2. If you rely on a network dongle, plug it (via OTG adapter) and check
     'scripts/dongle_status.sh' shows an interface with an IP.
Then re-run:  sudo $0 disable --confirm
(FORCE=1 skips all checks — only with physical access to the SD card.)
EOF
    else
        cat >&2 <<EOF
Refusing to disable onboard WiFi without --confirm. On this board there is no
USB gadget dev link, so the dongle IS the only network path. Verify first:
  1. 'scripts/dongle_status.sh' shows the dongle interface with an IP and the
     default route pointing at it.
  2. You can SSH in over that address right now (not over onboard WiFi).
  3. Recovery if it still goes wrong: GPIO serial console on pins 6/8/10
     (needs a USB-TTL adapter), or delete the marker block in config.txt with
     the SD card in a PC.
Then re-run:  sudo $0 disable --confirm
(FORCE=1 skips all checks — only with physical access to the SD card.)
EOF
    fi
    exit 1
}

disable() {
    [[ "${EUID}" -eq 0 ]] || die "run as root"
    local cfg
    cfg="$(boot_cfg)"
    if block_present; then
        log "already disabled in ${cfg} — reboot to apply if you haven't"
        return 0
    fi
    if [[ "${FORCE:-}" != "1" ]]; then
        # The lockout guard: something other than the onboard radio has to be
        # able to reach this box afterwards. A live USB dongle counts on every
        # board; on the Zero 2W a staged USB gadget dev link counts too. The
        # gadget used to be the *only* thing accepted there, which refused
        # perfectly safe boards that were sitting on an ethernet dongle with no
        # gadget staged.
        local uplink gadget_ready=false
        uplink="$(usb_uplink_iface)"
        if board_supports_gadget \
            && grep -q "dtoverlay=dwc2,dr_mode=otg" "$cfg" \
            && systemctl is-enabled usb-gadget.service >/dev/null 2>&1; then
            gadget_ready=true
        fi
        if [[ -n "$uplink" ]]; then
            log "USB uplink present: ${uplink} — safe to drop onboard WiFi"
        elif $gadget_ready; then
            log "USB gadget dev link staged — safe to drop onboard WiFi (tether to recover)"
        elif board_supports_gadget; then
            die "no USB dongle with carrier + IP and no staged USB dev link (run install.sh first), or FORCE=1 with SD-card access"
        else
            die "no USB network dongle with carrier + IP — plug it in first (scripts/dongle_status.sh), or FORCE=1 with SD-card access"
        fi
        [[ "${1:-}" == "--confirm" ]] || checklist
    fi
    {
        echo ""
        echo "$BEGIN_MARK"
        echo "# Onboard WiFi off: BT gets the whole 2.4 GHz radio. BT unaffected."
        echo "dtoverlay=disable-wifi"
        echo "$END_MARK"
    } >> "$cfg"
    log "onboard WiFi disabled in ${cfg} — reboot to apply"
    # Name only the recovery paths this board actually has. Listing the USB
    # tether on a board with no gadget-capable port sends someone chasing a
    # link that cannot exist, at the one moment they are already locked out.
    if board_supports_gadget; then
        log "recovery: USB tether (ssh <user>@10.55.0.1 / serial ttyACM0),"
        log "          GPIO UART pins 6/8/10, or delete the marker block on the SD card"
    else
        log "recovery: with a WiFi adapter fitted, the setup AP rises by itself"
        log "          ~90 s after the uplink goes away — join \"${AP_SSID:-Safydo}\";"
        log "          otherwise GPIO UART pins 6/8/10, or delete the marker block"
        log "          on the SD card"
    fi
}

enable() {
    [[ "${EUID}" -eq 0 ]] || die "run as root"
    local cfg tmp
    cfg="$(boot_cfg)"
    if ! block_present; then
        log "onboard WiFi already enabled in ${cfg}"
        return 0
    fi
    tmp="$(mktemp)"
    awk -v b="$BEGIN_MARK" -v e="$END_MARK" \
        '$0==b{skip=1; next} $0==e{skip=0; next} !skip{print}' "$cfg" > "$tmp"
    cp "$tmp" "$cfg"
    rm -f "$tmp"
    log "onboard WiFi re-enabled in ${cfg} — reboot to apply"
}

case "${1:-}" in
    disable) shift; disable "$@" ;;
    enable)  enable ;;
    status)  status ;;
    *)       echo "Usage: $0 status|disable [--confirm]|enable" >&2; exit 1 ;;
esac

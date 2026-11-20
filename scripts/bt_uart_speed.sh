#!/usr/bin/env bash
# SAFYDO — set the speed of the UART between the Pi and its onboard BT chip.
#
# Why: on the 3 A+ the BCM4345C0 wedges ("hci0: hardware error 0x00", then
# HCI_Reset timing out) every 20 min to 2 h of playback, and only a reboot
# recovers it (docs/BUILD.md). The stock link runs at the chip's ceiling,
# 3,000,000 baud. Running it slower gives the chip more slack per byte; this is
# an experiment to see whether the wedge rate drops.
#
# Quality is not affected: the codec sets the audio bitrate, the UART only
# carries it. SBC-XQ (~570 kbit/s) plus HCI/L2CAP overhead uses about a third of
# a 2.4 Mbaud line, and LDAC 990 kbit/s still fits under half.
#
# The value is written as `dtparam=krnbt_baudrate=N` under this board's
# config.txt filter ([pi3] / [pi02]) between SAFYDO markers. install.sh never
# touches the block, so it survives deploys. Takes effect at the next boot.
#
#   bt_uart_speed.sh status        what config.txt asks for vs what is live
#   bt_uart_speed.sh set BAUD      e.g. 2400000 (80 %); refused unless the Pi's
#                                  UART clock can produce it within 1 %
#   bt_uart_speed.sh reset         remove the block (back to the default 3 Mbaud)
#
# If the chip refuses the speed, hci0 never comes up after the reboot. The box
# stays reachable over the network: run `sudo bt_uart_speed.sh reset` and
# reboot. SD-card recovery: delete the block between the markers in config.txt.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"  # log/warn/die, boot_config_path, board_config_filter

BEGIN_MARK="# --- SAFYDO bt-uart-speed begin ---"
END_MARK="# --- SAFYDO bt-uart-speed end ---"
DEFAULT_BAUD=3000000
DT_SPEED=/proc/device-tree/soc/serial@7e201000/bluetooth/max-speed

boot_cfg() {
    local cfg
    cfg="$(boot_config_path)"
    [[ -n "$cfg" ]] || die "config.txt not found"
    echo "$cfg"
}

configured() {
    sed -n "/^${BEGIN_MARK}\$/,/^${END_MARK}\$/s/^dtparam=krnbt_baudrate=\([0-9]\+\)\$/\1/p" "$(boot_cfg)" | tail -1
}

live() {
    [[ -r "$DT_SPEED" ]] || { echo "unknown"; return; }
    od -An -tu4 --endian=big "$DT_SPEED" | tr -d ' '
}

uart_clock() {
    local hex
    hex="$(vcgencmd get_config init_uart_clock 2>/dev/null | cut -d= -f2)"
    [[ "$hex" =~ ^0x[0-9a-fA-F]+$ ]] && echo $((hex)) || echo 48000000
}

# PL011: baud = clk / (16 * (IBRD + FBRD/64)), i.e. clk*4 / divisor in 1/64ths.
check_baud() {
    local baud="$1" clk div actual err_ppm
    clk="$(uart_clock)"
    div=$(( (clk * 4 + baud / 2) / baud ))
    (( div >= 64 )) || die "${baud} baud is above what a ${clk} Hz UART clock can produce"
    actual=$(( clk * 4 / div ))
    err_ppm=$(( (actual > baud ? actual - baud : baud - actual) * 1000000 / baud ))
    (( err_ppm <= 10000 )) || die "${baud} baud would really be ${actual} (${err_ppm} ppm off) — pick another"
    log "PL011 at ${clk} Hz: ${baud} baud → divisor $((div / 64)) + $((div % 64))/64, actual ${actual} (${err_ppm} ppm)"
}

strip_block() {
    local cfg="$1" tmp
    tmp="$(mktemp)"
    awk -v b="$BEGIN_MARK" -v e="$END_MARK" '
        $0 == b { skip = 1; next }
        skip && $0 == e { skip = 0; next }
        !skip { print }
    ' "$cfg" > "$tmp"
    cat "$tmp" > "$cfg"   # keep the file's owner/mode (FAT anyway)
    rm -f "$tmp"
}

cmd_status() {
    local c
    c="$(configured)"
    echo "config.txt : ${c:-default (${DEFAULT_BAUD})}"
    echo "live (DT)  : $(live)"
    echo "hci0       : $(hciconfig hci0 2>/dev/null | sed -n 3p | xargs || echo missing)"
}

cmd_set() {
    local baud="${1:-}" cfg filter
    [[ "$baud" =~ ^[0-9]+$ ]] || die "usage: $0 set BAUD"
    (( baud >= 115200 && baud <= DEFAULT_BAUD )) || die "BAUD must be 115200..${DEFAULT_BAUD}"
    [[ $EUID -eq 0 ]] || die "run as root"
    check_baud "$baud"
    cfg="$(boot_cfg)"
    filter="$(board_config_filter)"
    cp -p "$cfg" "/var/tmp/config.txt.bak-bt-uart-$(date +%Y%m%d-%H%M%S)"
    strip_block "$cfg"
    {
        echo "$BEGIN_MARK"
        [[ -n "$filter" ]] && echo "$filter"
        echo "dtparam=krnbt_baudrate=${baud}"
        [[ -n "$filter" ]] && echo "[all]"
        echo "$END_MARK"
    } >> "$cfg"
    sync
    log "BT UART set to ${baud} baud in ${cfg}${filter:+ under ${filter}} — reboot to apply"
}

cmd_reset() {
    [[ $EUID -eq 0 ]] || die "run as root"
    local cfg
    cfg="$(boot_cfg)"
    grep -qF "$BEGIN_MARK" "$cfg" || { log "no SAFYDO BT UART block — already default"; return 0; }
    strip_block "$cfg"
    sync
    log "BT UART back to the default (${DEFAULT_BAUD}) — reboot to apply"
}

case "${1:-status}" in
    status) cmd_status ;;
    set)    shift; cmd_set "$@" ;;
    reset)  cmd_reset ;;
    *)      die "usage: $0 status|set BAUD|reset" ;;
esac

#!/usr/bin/env bash
# Recover Pi WiFi from mounted SD card (boot + root partitions on dev PC).
#
# Usage:
#   sudo ./scripts/sd_recover_wifi.sh /media/<you>/bootfs /media/<you>/rootfs
set -euo pipefail

BOOT="${1:-}"
ROOT="${2:-}"

usage() {
    cat <<'EOF'
Usage: sudo sd_recover_wifi.sh [BOOT_MOUNT] [ROOT_MOUNT]

Fixes:
  - cmdline.txt (restore .bak or strip corruption)
  - NetworkManager preconfigured WiFi — reset IPv4/DNS to DHCP auto
  - Clear stale NetworkManager runtime state
  - Remove wifi-powersave-off drop-in
  - Add dtoverlay=dwc2,dr_mode=host for Pi Zero 2W USB Ethernet
  - Fix /etc/hosts player.local line
EOF
}

find_mounts() {
    local u
    u="$(logname 2>/dev/null || echo "${SUDO_USER:-$USER}")"
    for b in "/media/${u}/bootfs" "/media/${u}/boot" "/mnt/boot"; do
        [[ -f "${b}/cmdline.txt" || -f "${b}/firmware/cmdline.txt" ]] || continue
        BOOT="$b"
        local parent
        parent="$(dirname "$b")"
        for r in "${parent}/rootfs" "${parent}/rootfs2" "${parent}/root"; do
            [[ -d "${r}/etc" ]] && ROOT="$r" && return 0
        done
        return 0
    done
    return 1
}

cmdline_path() {
    if [[ -f "${BOOT}/firmware/cmdline.txt" ]]; then
        echo "${BOOT}/firmware/cmdline.txt"
    elif [[ -f "${BOOT}/cmdline.txt" ]]; then
        echo "${BOOT}/cmdline.txt"
    else
        return 1
    fi
}

fix_cmdline() {
    local f bak
    f="$(cmdline_path)" || { echo "cmdline.txt not found under ${BOOT}" >&2; return 1; }
    bak="${f}.bak"
    echo "==> cmdline: ${f}"

    if [[ -f "$bak" ]]; then
        cp "$bak" "$f"
        echo "    restored from ${bak}"
        return 0
    fi

    local line
    line="$(tr '\n' ' ' < "$f" | sed 's/\[connection\].*//; s/wifi\.powersave.*//; s/wifi_pwr_mgmt=off[^ ]*//g; s/[[:space:]]\+/ /g; s/^[[:space:]]*//; s/[[:space:]]*$//')"
    printf '%s\n' "$line" > "$f"
    echo "    cleaned in place"
}

fix_config_usb() {
    local cfg="${BOOT}/config.txt"
    [[ -f "$cfg" ]] || cfg="${BOOT}/firmware/config.txt"
    [[ -f "$cfg" ]] || { echo "==> config.txt not found — skip USB OTG"; return 0; }

    echo "==> config: ${cfg}"
    # Any dwc2 line already decides the port's role. install.sh stages
    # dr_mode=otg for the USB dev link, and adding dr_mode=host beside it
    # turned that recovery link off — on a box being recovered.
    if grep -qE '^[[:space:]]*dtoverlay=dwc2' "$cfg" 2>/dev/null; then
        echo "    dwc2 overlay already configured — left alone"
        return 0
    fi

    # Appended under its own [all] at the end: `sed /^\[all\]/a` wrote the
    # line after EVERY [all] in the file.
    printf '\n[all]\ndtoverlay=dwc2,dr_mode=host\n' >> "$cfg"
    echo "    added dtoverlay=dwc2,dr_mode=host under [all] (Pi Zero 2W USB Ethernet)"
}

fix_hosts() {
    local hosts="${ROOT}/etc/hosts"
    [[ -f "$hosts" ]] || return 0
    echo "==> hosts: ${hosts}"
    if grep -qE '^127\.0\.1\.1[[:space:]]+safydo\.local' "$hosts"; then
        echo "    safydo.local already set"
        return 0
    fi
    # sed -i succeeds whether or not it matched, so an `|| append` fallback
    # never ran: decide on the line's presence instead.
    # Append only when there is no 127.0.1.1 line at all — one naming another
    # host (a renamed board) is not this script's to second-guess.
    if grep -qE '^127\.0\.1\.1[[:space:]]*safydo$' "$hosts"; then
        sed -i 's/^127\.0\.1\.1[[:space:]]*safydo$/127.0.1.1\tsafydo.local\tsafydo/' "$hosts"
    elif ! grep -qE '^127\.0\.1\.1[[:space:]]' "$hosts"; then
        echo -e '127.0.1.1\tsafydo.local\tsafydo' >> "$hosts"
    else
        echo "    127.0.1.1 names another host — left alone"
        return 0
    fi
    echo "    fixed 127.0.1.1 line"
}

remove_nm_powersave() {
    local dst="${ROOT}/etc/NetworkManager/conf.d/wifi-powersave-off.conf"
    if [[ -f "$dst" ]]; then
        rm -f "$dst"
        echo "==> removed ${dst}"
    else
        echo "==> NM powersave drop-in not present"
    fi
}

fix_nm_preconfigured() {
    local f="${ROOT}/etc/NetworkManager/system-connections/preconfigured.nmconnection"
    [[ -f "$f" ]] || { echo "==> no preconfigured.nmconnection — WiFi must be re-added via Imager"; return 1; }

    echo "==> NM WiFi profile: ${f}"
    echo "--- before ---"
    sed 's/\(psk=\).*/\1***REDACTED***/' "$f" || true

    if ! grep -q '^\[wifi\]' "$f" || ! grep -q '^ssid=' "$f"; then
        echo "ERROR: [wifi] or ssid missing — profile is broken; reflash with Pi Imager WiFi settings" >&2
        return 1
    fi

    local tmp
    tmp="$(mktemp)"
    awk '
        BEGIN { in_ipv4=0 }
        /^\[ipv4\]/ { in_ipv4=1; print; print "method=auto"; next }
        /^\[/ { in_ipv4=0 }
        in_ipv4 && /^(method|dns|dns-search|dns-options|dns-priority|ignore-auto-dns|address|gateway|route)/ { next }
        { print }
    ' "$f" > "$tmp"

    if ! grep -q '^\[ipv4\]' "$tmp"; then
        printf '\n[ipv4]\nmethod=auto\n' >> "$tmp"
    fi

    cp "$tmp" "$f"
    rm -f "$tmp"
    chmod 600 "$f"
    chown root:root "$f"

    echo "--- after ---"
    sed 's/\(psk=\).*/\1***REDACTED***/' "$f" || true
    echo "    reset IPv4 to DHCP auto (removed manual DNS overrides)"
}

unmask_wpa_supplicant() {
    local wants="${ROOT}/etc/systemd/system/wpa_supplicant.service"
    if [[ -L "$wants" && "$(readlink "$wants" 2>/dev/null)" == /dev/null ]]; then
        rm -f "$wants"
        echo "==> unmasked wpa_supplicant (partial install may have masked it)"
    else
        echo "==> wpa_supplicant not masked"
    fi
}

clear_nm_state() {
    local d="${ROOT}/var/lib/NetworkManager"
    [[ -d "$d" ]] || { echo "==> no ${d}"; return 0; }
    echo "==> clearing stale NM state in ${d}"
    find "$d" -mindepth 1 -maxdepth 1 -type f -delete 2>/dev/null || true
    find "$d" -mindepth 1 -maxdepth 1 -type d ! -name 'system-connections' -exec rm -rf {} + 2>/dev/null || true
}

main() {
    [[ "${EUID}" -eq 0 ]] || { echo "Run as root: sudo $0 /media/<you>/bootfs /media/<you>/rootfs" >&2; exit 1; }

    if [[ $# -eq 0 ]]; then
        find_mounts || { usage; exit 1; }
    fi

    [[ -n "$BOOT" && -n "$ROOT" ]] || { usage; exit 1; }
    echo "BOOT=${BOOT}"
    echo "ROOT=${ROOT}"
    echo ""

    fix_cmdline
    fix_config_usb
    fix_hosts
    remove_nm_powersave
    unmask_wpa_supplicant
    fix_nm_preconfigured
    clear_nm_state

    echo ""
    echo "Done. Eject SD safely, boot Pi, wait ~90s."
    echo "  ping safydo.local"
    echo "  ssh <user>@safydo.local 'curl -sI https://google.com | head -1'"
}

main "$@"
#!/usr/bin/env bash
# SAFYDO — idempotent installer
# Phase 0–1: base OS optimizations, Bluetooth audio, project deploy
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly INSTALL_DIR="/opt/rpi-audio-player"
readonly STATE_DIR="/var/lib/audio-player"
readonly RUN_DIR="/run/audio-player"
readonly MARKER_DIR="/var/lib/audio-player/install"
# Hostname resolution, in order: SAFYDO_HOSTNAME, whatever the box already
# calls itself, then "safydo". Two boards both claiming "safydo" on one LAN make
# avahi rename one to safydo-2.local and break SSH known_hosts, so a second
# board needs its own name — set it in Pi Imager or pass
# SAFYDO_HOSTNAME=safydo3a. Keeping the current name means re-running install.sh
# never renames a board back underneath you.
_default_hostname() {
    local current
    current="$(tr -d '[:space:]' < /etc/hostname 2>/dev/null || true)"
    case "$current" in
        ""|raspberrypi|localhost) echo safydo ;;
        *)                        echo "$current" ;;
    esac
}
readonly HOSTNAME="${SAFYDO_HOSTNAME:-$(_default_hostname)}"
readonly PHASE0_MARKER="${MARKER_DIR}/phase0.done"
readonly PHASE1_MARKER="${MARKER_DIR}/phase1.done"
readonly PHASE2_MARKER="${MARKER_DIR}/phase2.done"
readonly PHASE3_MARKER="${MARKER_DIR}/phase3.done"
readonly PHASE4_MARKER="${MARKER_DIR}/phase4.done"
readonly PHASE5_MARKER="${MARKER_DIR}/phase5.done"

# Board/OS detection: platform_board (zero2w|pi3aplus|other),
# platform_os (bookworm|trixie|other) + board_* capability predicates.
# shellcheck source=scripts/platform.sh
source "${SCRIPT_DIR}/scripts/platform.sh"

UPDATE_ONLY=false
SKIP_APT=false
DRY_RUN=false
NO_UPGRADE=false
# off|auto|keep — see apply_onboard_wifi_policy(). The default is "off": every
# board here shares one 2.4 GHz radio between WiFi and BT, so an install that
# leaves the radio on ships a machine that stutters under A2DP. It still refuses
# to cut the only way back in, and says so loudly when it defers.
ONBOARD_WIFI="${ONBOARD_WIFI:-off}"
# Units to leave alone even if slim_os_services() would normally disable them,
# e.g. SAFYDO_KEEP_SERVICES="man-db.timer apt-daily.timer"
KEEP_SERVICES="${SAFYDO_KEEP_SERVICES:-}"
APT_UPDATED=false
REBOOT_REQUIRED=false
# Set when the onboard-WiFi policy wanted to act and could not. Reprinted at the
# very end: a warning 25 minutes up the scrollback has not been read.
ONBOARD_WIFI_DEFERRED=""
BT_WEDGE_NOTE=""
BT_WEDGE_STATE="unknown"
BT_WEDGE_ADVISE=false

log()  { echo "[install] $*"; }
warn() { echo "[install] WARNING: $*" >&2; }
die()  { echo "[install] ERROR: $*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: sudo ./install.sh [OPTIONS]

Idempotent setup for Raspberry Pi OS Lite 32-bit.
Detects board x OS: Zero 2W/Bookworm and Pi 3 A+/Trixie (scripts/platform.sh).

Options:
  --update       Refresh apt packages and redeploy configs only
  --skip-apt     Skip apt update/install (config deploy only)
  --no-upgrade   Skip the full system dist-upgrade (packages still install)
  --dry-run      Print actions without modifying the system
  -h, --help     Show this help

Onboard WiFi (the 2.4 GHz radio BT has to share on every supported board):
  --disable-onboard-wifi   Disable it (the default; reboot to apply)
  --keep-onboard-wifi      Never touch it
  ONBOARD_WIFI=auto        Only disable when a USB uplink is already carrying
                           this box (the pre-2026-08-08 conservative rule)
  (default: off — disabled on every board and OS, unless doing so would leave
   no way back in, which is reported loudly instead)

Environment:
  SAFYDO_HOSTNAME=name        Board name for mDNS (two boards must not share one)
  SAFYDO_KEEP_SERVICES="a b"  Units the slimming pass must not disable
  PURGE_PRINTING=1            apt-purge printing packages instead of masking
  APPLY_OVERCLOCK=1           Opt into the per-board overclock fragment
  SWAP_POLICY=keep            Leave the image's swap alone (default: zram only, nothing on SD)
  MPV_JEMALLOC=0              Run mpv on glibc malloc instead of jemalloc

Re-run safely at any time. Phase 0 is applied once unless markers are cleared.
EOF
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --update)    UPDATE_ONLY=true ;;
            --skip-apt)  SKIP_APT=true ;;
            --no-upgrade) NO_UPGRADE=true ;;
            --disable-onboard-wifi) ONBOARD_WIFI=off ;;
            --keep-onboard-wifi)    ONBOARD_WIFI=keep ;;
            --dry-run)   DRY_RUN=true ;;
            -h|--help)   usage; exit 0 ;;
            *)           die "Unknown option: $1 (try --help)" ;;
        esac
        shift
    done
}

run() {
    if $DRY_RUN; then
        log "[dry-run] $*"
    else
        log "+ $*"
        "$@"
    fi
}

run_sh() {
    if $DRY_RUN; then
        log "[dry-run] $*"
    else
        log "+ $*"
        eval "$@"
    fi
}

require_root() {
    [[ "${EUID}" -eq 0 ]] || die "Run as root: sudo ./install.sh"
}

detect_pi() {
    log "Detected: $(platform_describe)"
    if [[ ! -f /proc/device-tree/model ]]; then
        warn "Not running on Raspberry Pi hardware — continuing for dev/testing"
    fi
    case "$(platform_board)" in
        zero2w|pi3aplus) : ;;
        *) warn "Unrecognised board — board-specific tuning (overclock, USB gadget) is skipped" ;;
    esac
    case "$(platform_os)" in
        bookworm|trixie) : ;;
        *) warn "Unrecognised OS release — assuming Debian-like package names" ;;
    esac
    $DRY_RUN || platform_write_cache
}

boot_config_path() {
    if [[ -f /boot/firmware/config.txt ]]; then
        echo /boot/firmware/config.txt
    elif [[ -f /boot/config.txt ]]; then
        echo /boot/config.txt
    else
        echo ""
    fi
}

append_fragment() {
    local target="$1"
    local fragment="$2"
    local marker="$3"

    [[ -f "$fragment" ]] || die "Missing fragment: $fragment"

    if [[ -f "$target" ]] && grep -qF "$marker" "$target" 2>/dev/null; then
        log "Already applied: ${target} (${marker})"
        return 0
    fi

    run mkdir -p "$(dirname "$target")"
    if $DRY_RUN; then
        log "[dry-run] append ${fragment} -> ${target}"
        return 0
    fi

    {
        echo ""
        echo "# ${marker}"
        cat "$fragment"
    } >> "$target"
    log "Appended ${fragment} to ${target}"
}

# Same as append_fragment, but scoped to the running board with a config.txt
# conditional filter ([pi02] / [pi3]) and closed with [all] so nothing appended
# later inherits the filter. Keeps an SD card honest if it is moved between the
# Zero 2W and the 3 A+.
append_fragment_for_board() {
    local target="$1" fragment="$2" marker="$3"
    local filter
    filter="$(board_config_filter)"

    if [[ -z "$filter" ]]; then
        append_fragment "$target" "$fragment" "$marker"
        return 0
    fi

    [[ -f "$fragment" ]] || die "Missing fragment: $fragment"
    if [[ -f "$target" ]] && grep -qF "$marker" "$target" 2>/dev/null; then
        log "Already applied: ${target} (${marker})"
        return 0
    fi

    run mkdir -p "$(dirname "$target")"
    if $DRY_RUN; then
        log "[dry-run] append ${fragment} (${filter}) -> ${target}"
        return 0
    fi

    {
        echo ""
        echo "# ${marker}"
        echo "$filter"
        cat "$fragment"
        echo "[all]"
    } >> "$target"
    log "Appended ${fragment} to ${target} under ${filter}"
}

remove_usb_host_block() {
    # Strip the legacy "RPI Audio Player USB host" fragment (dtoverlay=dwc2,
    # dr_mode=host). Two dwc2 overlays conflict — the firmware keeps the first
    # (host) and the OTG dev link's dr_mode=otg never takes effect. Bounded so
    # it only removes the injected block, never trailing config.
    local cfg="$1" tmp
    if $DRY_RUN; then
        log "[dry-run] strip obsolete USB-host overlay block from ${cfg}"
        return 0
    fi
    tmp="$(mktemp)"
    awk '
        skip {
            if ($0 ~ /^dtoverlay=dwc2,dr_mode=host/) { skip=0; next }
            if ($0 == "" || $0 ~ /^# RPI Audio Player/) { skip=0; print; next }
            next
        }
        /^# RPI Audio Player USB host$/ { skip=1; next }
        { print }
    ' "$cfg" > "$tmp"
    cat "$tmp" > "$cfg"
    rm -f "$tmp"
    log "Removed obsolete USB-host overlay block from ${cfg}"
}

apply_boot_config() {
    local cfg
    cfg="$(boot_config_path)"
    [[ -n "$cfg" ]] || { warn "config.txt not found — skip boot tuning"; return 0; }

    append_fragment "$cfg" \
        "${SCRIPT_DIR}/docs/os-tuning/config.txt.fragment" \
        "RPI Audio Player (Phase 0)"

    # Overclock applies only after reboot — off by default (WiFi-safe on Zero 2W,
    # heat-safe on the bare 3 A+). The fragment is per board: the Zero 2W gains
    # from 1000 -> 1350 MHz, the 3 A+ already runs at 1400 and only throttles.
    local oc_fragment
    oc_fragment="$(board_overclock_fragment)"
    if [[ "${APPLY_OVERCLOCK:-}" == "1" ]]; then
        if [[ -n "$oc_fragment" ]]; then
            append_fragment_for_board "$cfg" \
                "${SCRIPT_DIR}/docs/os-tuning/${oc_fragment}" \
                "RPI Audio Player overclock"
        else
            warn "No overclock profile for this board — skipping"
        fi
    else
        log "Skipping overclock (set APPLY_OVERCLOCK=1 for ${oc_fragment:-none})"
    fi

    # Boards without the USB gadget dev link still get the GPIO serial console
    # as the lockout fallback (the gadget fragment carries enable_uart=1 itself).
    if ! board_supports_gadget; then
        append_fragment "$cfg" \
            "${SCRIPT_DIR}/docs/os-tuning/config-serial-console.fragment" \
            "RPI Audio Player serial console"
    fi
    # Only the install.sh-appended fragment is a hazard (unguarded dwc2 host
    # overlay). The stock Raspberry Pi OS 'dtoverlay=dwc2,dr_mode=host' lives
    # under a [cm5] filter and is inert on other models — never touch it.
    if grep -qF "# RPI Audio Player USB host" "$cfg" 2>/dev/null; then
        warn "obsolete 'RPI Audio Player USB host' overlay in ${cfg} conflicts with the OTG dev link — removing it"
        remove_usb_host_block "$cfg"
    fi
}

apply_usb_gadget() {
    # USB dev link (docs/usb-dev-link.md): dwc2 in OTG mode + composite gadget
    # (serial console ttyGS0 + ethernet usb0 @ 10.55.0.1). Recovery/dev access
    # once onboard WiFi is disabled; port still works as USB host via OTG adapter.
    if [[ "${SKIP_USB_GADGET:-}" == "1" ]]; then
        log "SKIP_USB_GADGET=1 — skip USB dev link"
        return 0
    fi
    # Zero 2W only. The 3 A+ grounds the OTG ID pin (host-only unless
    # dr_mode=peripheral + a USB-A-to-A cable with the power line cut), and the
    # dwc2 overlay would replace the FIQ-driven dwc_otg host driver — a throughput
    # hit on the very port the network dongle streams over. Serial console
    # (enable_uart=1) is the recovery path there instead.
    if ! board_supports_gadget; then
        log "USB dev link not applicable on $(platform_board) — skipping (GPIO serial console is the fallback)"
        return 0
    fi
    local cfg
    cfg="$(boot_config_path)"
    [[ -n "$cfg" ]] || { warn "config.txt not found — skip USB gadget"; return 0; }

    append_fragment "$cfg" \
        "${SCRIPT_DIR}/docs/os-tuning/config-usb-gadget.fragment" \
        "RPI Audio Player USB gadget (dev link)"

    # NM owns usb0: static 10.55.0.1/24, never a default route.
    if command -v nmcli >/dev/null 2>&1; then
        if ! nmcli -t -f NAME connection show 2>/dev/null | grep -qx "safydo-usb-gadget"; then
            run nmcli connection add type ethernet ifname usb0 con-name safydo-usb-gadget \
                ipv4.method manual ipv4.addresses 10.55.0.1/24 ipv4.never-default yes \
                ipv6.method disabled connection.autoconnect yes \
                connection.autoconnect-priority 100 2>/dev/null || \
                warn "could not create NM profile safydo-usb-gadget"
        fi
    fi

    run systemctl enable usb-gadget.service 2>/dev/null || true
    run systemctl enable usb-gadget-dhcp.service 2>/dev/null || true
    run systemctl enable serial-getty@ttyGS0.service 2>/dev/null || true

    if [[ -n "$(ls /sys/class/udc 2>/dev/null)" ]]; then
        run systemctl start usb-gadget.service 2>/dev/null || true
        run systemctl start usb-gadget-dhcp.service 2>/dev/null || true
        log "USB dev link active (serial ttyGS0 + usb0 10.55.0.1)"
    else
        log "USB dev link staged — active after next reboot (dwc2 overlay)"
    fi
}

configure_networkmanager_wifi() {
    # Bookworm uses NetworkManager for WiFi. On full install, mask global wpa_supplicant
    # so it does not fight NM on boot. On --skip-apt deploys, do NOT re-mask — sd_recover_wifi
    # and cold boot need the mask removed; re-masking broke sudo reboot on Zero 2W.
    if ! command -v NetworkManager >/dev/null 2>&1; then
        return 0
    fi
    run systemctl enable NetworkManager.service 2>/dev/null || true
    run nmcli device set wlan0 managed yes 2>/dev/null || true

    if $SKIP_APT; then
        local wants="/etc/systemd/system/wpa_supplicant.service"
        if [[ -L "$wants" && "$(readlink "$wants" 2>/dev/null)" == /dev/null ]]; then
            run rm -f "$wants"
            log "skip-apt: removed wpa_supplicant mask (reboot-safe deploy)"
        else
            log "skip-apt: left wpa_supplicant state unchanged"
        fi
        return 0
    fi

    # Do NOT --now: stopping wpa_supplicant drops SSH/WiFi mid-install on headless Pi.
    run systemctl disable wpa_supplicant.service 2>/dev/null || true
    run systemctl mask wpa_supplicant.service 2>/dev/null || true
    log "wpa_supplicant masked for next boot (NetworkManager manages WiFi; left running until reboot)"
}

ethernet_priority_enabled() {
    python3 -c "
import yaml
from pathlib import Path
p = Path('${INSTALL_DIR}/config/player.yaml')
if not p.is_file():
    p = Path('${SCRIPT_DIR}/config/player.yaml')
d = yaml.safe_load(p.read_text()) or {}
print(str((d.get('network') or {}).get('ethernet_priority', False)).lower())
" 2>/dev/null || echo "false"
}

apply_wifi_tuning() {
    # WiFi powersave drop-in is OPT-IN — default off for reboot safety on Zero 2W.
    # APPLY_WIFI_TUNING=1 installs docs/os-tuning/wifi-powersave-off.conf
    # SKIP_WIFI_TUNING=1 skips and removes the drop-in (deploy.sh sets this).
    # We never edit cmdline.txt (too risky headless).
    local dst="/etc/NetworkManager/conf.d/wifi-powersave-off.conf"

    if [[ "${SKIP_WIFI_TUNING:-}" == "1" || "${APPLY_WIFI_TUNING:-}" != "1" ]]; then
        if [[ -f "$dst" ]]; then
            run rm -f "$dst"
            log "Removed WiFi powersave drop-in (reboot-safe default): ${dst}"
        else
            log "WiFi powersave drop-in skipped (reboot-safe default; set APPLY_WIFI_TUNING=1 to enable)"
        fi
        return 0
    fi

    local src="${SCRIPT_DIR}/docs/os-tuning/wifi-powersave-off.conf"
    if [[ -f "$src" ]]; then
        if [[ -f "$dst" ]] && cmp -s "$src" "$dst" 2>/dev/null; then
            log "NetworkManager WiFi powersave config already up to date"
        else
            run mkdir -p /etc/NetworkManager/conf.d
            run cp "$src" "$dst"
            run chmod 644 "$dst"
            log "NetworkManager WiFi powersave disabled: ${dst}"
        fi
    fi
}

apply_sysctl() {
    local src="${SCRIPT_DIR}/docs/os-tuning/99-audio-player.conf"
    local dst="/etc/sysctl.d/99-audio-player.conf"

    if [[ -f "$dst" ]] && cmp -s "$src" "$dst" 2>/dev/null; then
        log "Sysctl already up to date: ${dst}"
    else
        run cp "$src" "$dst"
        run chmod 644 "$dst"
        run sysctl --system >/dev/null 2>&1 || run sysctl -p "$dst" || true
        log "Sysctl applied: ${dst}"
    fi
}

# Swap lives in compressed RAM only, never on the SD card (SWAP_POLICY=keep to
# leave the image's setup alone). Found 2026-11-20 on the 3 A+: Trixie's rpi-swap
# defaults to "zram+file", and its rpi-zram-writeback timer (3 h after boot, then
# daily) pushes idle swapped pages into a 472 MB /var/swap on the card. Bookworm
# images (Zero 2W) swap with dphys-swapfile, i.e. straight onto the card.
apply_swap_policy() {
    if [[ "${SWAP_POLICY:-zram}" == "keep" ]]; then
        log "SWAP_POLICY=keep — leaving the image's swap setup alone"
        return 0
    fi
    if [[ -f /etc/rpi/swap.conf ]]; then
        local dropin=/etc/rpi/swap.conf.d/90-safydo.conf
        local want=$'# SAFYDO: compressed RAM swap only, no writeback file on the SD card.\n[Main]\nMechanism=zram\n'
        if [[ "$(cat "$dropin" 2>/dev/null)"$'\n' == "$want" ]]; then
            log "rpi-swap already zram-only"
        else
            run mkdir -p /etc/rpi/swap.conf.d
            $DRY_RUN || printf '%s' "$want" > "$dropin"
            log "rpi-swap set to zram only (${dropin}); /var/swap is removed at the next boot"
        fi
        # The writeback would still run until that boot. Stop, not disable: the
        # timer is generated under /run by rpi-swap-generator (and no longer
        # generated once Mechanism=zram), so "disable" leaves it "failed".
        run systemctl stop rpi-zram-writeback.timer 2>/dev/null || true
        run systemctl reset-failed rpi-zram-writeback.timer 2>/dev/null || true
        return 0
    fi
    # Bookworm: zram via systemd-zram-generator, then retire dphys-swapfile.
    if [[ -d /usr/lib/systemd/system-generators ]] && \
       [[ -x /usr/lib/systemd/system-generators/zram-generator ]]; then
        local conf=/etc/systemd/zram-generator.conf
        local want=$'# SAFYDO: compressed RAM swap (replaces dphys-swapfile on the SD card).\n[zram0]\nzram-size = ram\ncompression-algorithm = zstd\nswap-priority = 100\n'
        if [[ "$(cat "$conf" 2>/dev/null)"$'\n' != "$want" ]]; then
            $DRY_RUN || printf '%s' "$want" > "$conf"
            run systemctl daemon-reload
            log "zram swap configured (${conf})"
        fi
        run systemctl start systemd-zram-setup@zram0.service 2>/dev/null || true
        run systemctl start dev-zram0.swap 2>/dev/null || true
    else
        warn "no zram generator available — not touching the existing swap"
        return 0
    fi
    # Only once zram is really swapping: whatever sits in the file swap must
    # have somewhere to go when it is turned off.
    if command -v dphys-swapfile >/dev/null 2>&1 && grep -q '^/dev/zram0 ' /proc/swaps; then
        run dphys-swapfile swapoff 2>/dev/null || true
        run dphys-swapfile uninstall 2>/dev/null || true
        run systemctl disable --now dphys-swapfile.service 2>/dev/null || true
        log "dphys-swapfile retired — swap is zram only"
    fi
}

apply_avahi() {
    local src="${SCRIPT_DIR}/docs/os-tuning/avahi-daemon.conf"
    local dst="/etc/avahi/avahi-daemon.conf"

    if [[ -f "$dst" ]] && cmp -s "$src" "$dst" 2>/dev/null; then
        log "Avahi config already up to date"
    else
        run cp "$src" "$dst"
        log "Avahi config deployed"
    fi

    if ! $DRY_RUN; then
        hostnamectl set-hostname "$HOSTNAME" 2>/dev/null || echo "$HOSTNAME" > /etc/hostname
        if [[ -f /etc/hosts ]]; then
            if ! grep -qE "[[:space:]]${HOSTNAME}(\.[[:space:]]|$)" /etc/hosts; then
                sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t${HOSTNAME}.local\t${HOSTNAME}/" /etc/hosts 2>/dev/null || \
                    echo -e "127.0.1.1\t${HOSTNAME}.local\t${HOSTNAME}" >> /etc/hosts
            fi
        fi
    else
        log "[dry-run] set hostname to ${HOSTNAME}"
    fi

    run systemctl enable avahi-daemon.service
    run systemctl restart avahi-daemon.service 2>/dev/null || warn "avahi restart failed — check /etc/avahi/avahi-daemon.conf"
}

apply_fstab_tmpfs() {
    if [[ "${SKIP_FSTAB_TMPFS:-}" == "1" ]]; then
        log "SKIP_FSTAB_TMPFS=1 — skip tmpfs fstab"
        return 0
    fi

    local fstab="/etc/fstab"
    local fragment="${SCRIPT_DIR}/docs/os-tuning/fstab-tmpfs.fragment"
    local marker="RPI Audio Player — reduce SD writes"

    [[ -f "$fstab" ]] || { warn "No /etc/fstab — skip tmpfs"; return 0; }

    if grep -qF "$marker" "$fstab" 2>/dev/null; then
        log "fstab tmpfs already applied"
        return 0
    fi

    # Debian 13 mounts /tmp as tmpfs itself (systemd tmp.mount, up to 50 % of
    # RAM = 256 MB here). Our fstab entry still wins — systemd-fstab-generator
    # supersedes tmp.mount — and caps it at 64 MB, which is what a 512 MB board
    # wants. Logged so the pre-existing mount is not mistaken for our doing.
    if findmnt -no FSTYPE /tmp 2>/dev/null | grep -qx tmpfs; then
        log "/tmp is already tmpfs (systemd default on trixie) — fstab entry will cap it at 64M after reboot"
    fi

    if $DRY_RUN; then
        log "[dry-run] append tmpfs entries to fstab"
        return 0
    fi

    {
        echo ""
        echo "# ${marker}"
        grep -v '^#' "$fragment" | grep -v '^[[:space:]]*$' || true
    } >> "$fstab"
    log "tmpfs entries added to fstab (effective after reboot)"
}

apply_tmpfs_log_dirs() {
    # /var/log on tmpfs is empty after boot — nginx needs /var/log/nginx before nginx -t.
    local src="${SCRIPT_DIR}/docs/os-tuning/tmpfiles-log-dirs.conf"
    local dst="/etc/tmpfiles.d/audio-player-logs.conf"
    if [[ -f "$src" ]]; then
        if [[ -f "$dst" ]] && cmp -s "$src" "$dst" 2>/dev/null; then
            :
        else
            run cp "$src" "$dst"
            run chmod 644 "$dst"
            log "tmpfiles log-dir config deployed: ${dst}"
        fi
    fi
    run mkdir -p /var/log/nginx
    run chown www-data:adm /var/log/nginx 2>/dev/null || true
    if command -v systemd-tmpfiles >/dev/null 2>&1; then
        run systemd-tmpfiles --create "$dst" 2>/dev/null || run systemd-tmpfiles --create 2>/dev/null || true
    fi
}

tune_root_filesystem() {
    local root_dev
    root_dev="$(findmnt -n -o SOURCE / 2>/dev/null || true)"
    [[ -n "$root_dev" ]] || { warn "Cannot detect root FS — skip tune2fs"; return 0; }

    # 5s commit interval: balances power-loss safety vs SD wear
    if tune2fs -l "$root_dev" 2>/dev/null | grep -q "Maximum mount count"; then
        run tune2fs -o journal_data_ordered "$root_dev" 2>/dev/null || true
        run tune2fs -c 0 -i 0 "$root_dev" 2>/dev/null || true
        log "ext4 tuned on ${root_dev} (journal_data_ordered, no forced fsck interval)"
    fi
}

# True when apt has an installable candidate. Mirrors apt_pkg_available() in
# _common.sh, which install.sh cannot source (readonly SCRIPT_DIR/INSTALL_DIR).
apt_available() {
    apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/{print $2}' | grep -qv '^(none)$'
}

# Install what apt can actually resolve, and say plainly what it could not.
#
# `apt-get install` is all-or-nothing: one unresolvable name makes it reject the
# entire list. On the first 3 A+ install a single optional package
# (libfdk-aac-dev — in Debian non-free, absent from Raspbian trixie armhf) took
# out mpv, nginx, FastAPI and bluez-alsa-utils in one go, and because the caller
# only warned, the run still reported success. Filtering first keeps one bad
# name from costing the other eighteen.
install_pkgs() {
    local p available=() missing=()
    for p in "$@"; do
        if apt_available "$p"; then available+=("$p"); else missing+=("$p"); fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        warn "No apt candidate on $(platform_os)/$(dpkg --print-architecture) — skipped: ${missing[*]}"
    fi
    if [[ ${#available[@]} -eq 0 ]]; then
        warn "Nothing installable in this package list"
        return 1
    fi
    run apt-get install -y -qq "${available[@]}"
}

# apt-get update is slow on a Pi over a dongle; every caller goes through here
# so a full run refreshes the index exactly once.
apt_update_once() {
    $APT_UPDATED && return 0
    run apt-get update -qq
    APT_UPDATED=true
}

# Bring the whole base OS current before anything is built on top of it.
#
# dist-upgrade rather than upgrade: plain upgrade refuses changes that add or
# remove packages, which is exactly how kernel/firmware transitions get held
# back — and a half-upgraded base is what breaks the bluez-alsa build later.
# noninteractive + confdef/confold so an unattended install can never park on a
# conffile prompt; our own drop-ins live in .d/ directories, so keeping the
# packaged conffile is always the right answer here.
system_upgrade() {
    if $SKIP_APT; then
        log "Skipping system upgrade (--skip-apt)"
        return 0
    fi
    if $NO_UPGRADE; then
        log "Skipping system upgrade (--no-upgrade)"
        return 0
    fi
    export DEBIAN_FRONTEND=noninteractive
    apt_update_once
    log "Upgrading base OS (dist-upgrade) — this is the slow part on a fresh image"
    run apt-get -y -qq \
        -o Dpkg::Options::=--force-confdef \
        -o Dpkg::Options::=--force-confold \
        dist-upgrade
    run apt-get -y -qq --purge autoremove
    run apt-get -y -qq autoclean
    log "Base OS up to date"
}

phase0_packages() {
    # Phase 0 minimal set; full stack installed in later phases
    local pkgs=(
        avahi-daemon
        avahi-utils
        openssh-server
        rsync
        curl
        jq
        htop
        rfkill
    )

    if $SKIP_APT; then
        log "Skipping apt (--skip-apt)"
        return 0
    fi

    apt_update_once
    # system_upgrade() already did the upgrading; --update just needs the
    # Phase 0 set present and current.
    install_pkgs "${pkgs[@]}"
}

# Packages for full install (Phases 1–7) — installed on first run, skipped with --update
full_packages() {
    if $SKIP_APT; then
        log "Skipping full package install (--skip-apt)"
        return 0
    fi
    local pkgs=(
        bluez
        bluez-tools
        bluez-alsa-utils
        mpv
        alsa-utils
        python3-fastapi
        python3-uvicorn
        python3-pydantic
        python3-yaml
        nginx
        hostapd
        dnsmasq
        wpasupplicant
        ntfs-3g
        exfatprogs
        udisks2
        cifs-utils
        nfs-common
        python3-rich
        # Web links: yt-dlp lives in a venv (install_ytdlp). Debian strips
        # ensurepip out of python3, so without this "python3 -m venv" fails.
        python3-venv
        # Recording encodes with the ffmpeg CLI (mpv only links the libraries).
        ffmpeg
        # mpv runs on jemalloc (mpv_daemon.sh): glibc kept a long stream's
        # freed demuxer cache, 320 MB of it on a 472 MB board.
        libjemalloc2
        # Persistent D-Bus to BlueZ for speaker volume (backend/services/
        # dbus_bluez.py) instead of forking dbus-send/busctl every second.
        python3-jeepney
        # Swap in compressed RAM on Bookworm (apply_swap_policy). Trixie ships
        # it already, as a dependency of rpi-swap.
        systemd-zram-generator
        # libgpiod tools. bt_rebind_verdict() reads which GPIO chip carries
        # BT_ON to decide whether unbinding the BT driver can power-cycle the
        # chip; without gpioinfo that probe is skipped and the verdict falls
        # back to weaker evidence. bt_recover.sh also reads BT_ON with gpioget.
        gpiod
    )
    # A packaged fdk-aac lets build_bluealsa.sh skip its slowest stage. Debian
    # ships libfdk-aac-dev in non-free; Raspbian trixie armhf does not — verified
    # on the 3 A+, non-free enabled, no candidate. Probe instead of assuming;
    # build_bluealsa.sh compiles 2.0.3 from source when it is absent.
    if apt_available libfdk-aac-dev; then
        pkgs+=(libfdk-aac-dev)
    fi
    install_pkgs "${pkgs[@]}" || warn "Package install failed — check apt sources"

    # Optional, installed separately so a missing package cannot take the whole
    # list down with it: python3-lgpio drives the Blinkt LED VU meter. Without
    # it backend/services/leds.py reports available=False and the rest runs on.
    if ! python3 -c "import lgpio" >/dev/null 2>&1; then
        run apt-get install -y -qq python3-lgpio || \
            warn "python3-lgpio unavailable — Blinkt LED meter stays disabled"
    fi
}

apply_bluetooth() {
    local src="${SCRIPT_DIR}/docs/os-tuning/bluetooth-main.conf"
    local dst="/etc/bluetooth/main.conf"
    local marker="RPI Audio Player (Phase 1)"

    # A stock image can ship with the BT radio soft-blocked, and systemd-rfkill
    # faithfully restores that block on every boot — so the adapter enumerates,
    # loads firmware, reports a BD address, and still refuses to power on with
    # org.bluez.Error.Failed / "Operation not possible due to RF-kill (132)".
    # Found on Raspberry Pi OS Trixie for the 3 A+: the persisted block was
    # dated to the image build itself. Unblocking is idempotent and harmless
    # where nothing is blocked, and systemd-rfkill persists the new state.
    if command -v rfkill >/dev/null 2>&1; then
        if rfkill list bluetooth 2>/dev/null | grep -q "Soft blocked: yes"; then
            run rfkill unblock bluetooth
            log "Bluetooth was rfkill soft-blocked (stock image state) — unblocked"
        fi
    else
        warn "rfkill not installed — cannot check whether the BT radio is soft-blocked"
    fi

    run mkdir -p /etc/bluetooth
    if [[ -f "$dst" ]] && grep -qF "$marker" "$dst" 2>/dev/null; then
        if ! cmp -s "$src" "$dst" 2>/dev/null; then
            run cp "$src" "$dst"
            log "Bluetooth main.conf updated"
        else
            log "Bluetooth main.conf already applied"
        fi
    else
        if [[ -f "$dst" ]]; then
            run cp "$dst" "${dst}.bak.$(date +%Y%m%d%H%M%S)"
        fi
        run cp "$src" "$dst"
        log "Bluetooth main.conf deployed"
    fi

    run systemctl enable bluetooth.service
    run systemctl start bluetooth.service 2>/dev/null || true
}

apply_bluealsa_hd_build() {
    # HD codecs (AAC/aptX/aptX-HD/LDAC/SBC-XQ + bluealsa-cli) need the
    # source-built bluez-alsa — Debian's package is SBC+LDAC only. Keeps
    # the one-command fresh-image install reproducing the documented state.
    if /usr/local/bin/bluealsa --help 2>&1 | grep -q "AAC"; then
        log "bluez-alsa HD build already installed — skipping source build"
        # The drop-in is only written by the build. xq is the default the
        # per-speaker switch (bt_prepare_bluealsa, _common.sh) is built on; a
        # 2026-11-14 build of this script set high here, so put it back.
        local dropin=/etc/systemd/system/bluealsa.service.d/10-hd-codecs.conf
        if grep -q -- '--sbc-quality=high' "$dropin" 2>/dev/null; then
            run sed -i 's/--sbc-quality=high/--sbc-quality=xq/' "$dropin"
            run systemctl daemon-reload
            run systemctl restart bluealsa.service
            log "bluealsa: default SBC quality back to xq (per-speaker override in /run)"
        fi
        return 0
    fi
    if [[ "${SKIP_BLUEALSA_BUILD:-0}" == "1" ]]; then
        warn "HD codec build skipped (SKIP_BLUEALSA_BUILD=1) — run scripts/build_bluealsa.sh later; BT stays SBC-only until then"
        return 0
    fi
    # The dominant cost is fdk-aac from source, needed whenever apt has no
    # libfdk-aac-dev candidate — the case on Raspbian armhf for both releases,
    # verified on a 3 A+/trixie. So the estimate tracks CPU speed, not the OS.
    if [[ "$(platform_board)" == "zero2w" ]]; then
        log "Building bluez-alsa with HD codecs (one-time, ~30-45 min on a Zero 2W)..."
    else
        log "Building bluez-alsa with HD codecs (one-time, ~20-30 min)..."
    fi
    run "${SCRIPT_DIR}/scripts/build_bluealsa.sh" || \
        warn "bluez-alsa HD build failed — BT works on the stock SBC-only stack; re-run scripts/build_bluealsa.sh"
}

apply_bluealsa() {
    local src="${SCRIPT_DIR}/docs/os-tuning/bluealsa"
    local dst="/etc/default/bluealsa"

    if [[ -f "$dst" ]] && cmp -s "$src" "$dst" 2>/dev/null; then
        log "bluealsa defaults already up to date"
    else
        run cp "$src" "$dst"
        log "bluealsa defaults deployed to ${dst}"
    fi

    apply_bluealsa_hd_build

    # Bookworm package uses bluealsa.service
    run systemctl enable bluealsa.service 2>/dev/null || \
        run systemctl enable bluealsa-aplay.service 2>/dev/null || \
        warn "bluealsa service unit not found — check bluez-alsa-utils install"
    # Start, don't restart: an unconditional restart on every install dropped the
    # connected speaker's stream, bt_connect.sh then saw "connected, no A2DP"
    # and restarted bluetoothd to heal it — losing unbonded pairings, and the
    # speaker came back by itself without AVRCP volume. Real config changes
    # (the HD build, the xq migration above) restart bluealsa themselves.
    if systemctl is-active --quiet bluealsa.service 2>/dev/null; then
        log "bluealsa already running — not restarting it"
    else
        run systemctl start bluealsa.service 2>/dev/null || \
            run systemctl start bluealsa-aplay.service 2>/dev/null || true
        # mpv holds a stale bluealsa PCM if bluealsa (re)started under it
        run systemctl try-restart audio-player-mpv.service 2>/dev/null || true
    fi
}

enable_phase1_services() {
    run systemctl enable audio-player-bt-autoconnect.service 2>/dev/null || true
    run systemctl enable audio-player-bt-retry.timer 2>/dev/null || true
    run systemctl start audio-player-bt-retry.timer 2>/dev/null || true
    # The controller can stop answering mid-stream while every layer above it
    # still reports healthy; without this the box plays silence until someone
    # reboots it. Board-agnostic: the watchdog tries the rebind on any board and
    # only escalates where the config says it may.
    run systemctl enable audio-player-bt-watchdog.timer 2>/dev/null || true
    run systemctl start audio-player-bt-watchdog.timer 2>/dev/null || true
    log "Bluetooth auto-connect service + retry timer + wedge watchdog enabled"
}

enable_phase2_services() {
    if command -v mpv >/dev/null 2>&1; then
        run systemctl enable audio-player-mpv.service 2>/dev/null || true
        run systemctl enable audio-player-radio.service 2>/dev/null || true
        log "mpv player service enabled"
    else
        warn "mpv not installed — skip audio-player-mpv.service"
    fi
}

enable_phase3_services() {
    if python3 -c "import fastapi, uvicorn" 2>/dev/null; then
        run systemctl enable audio-player-backend.service 2>/dev/null || true
        run systemctl restart audio-player-backend.service 2>/dev/null || true
        log "FastAPI backend service enabled"
    else
        warn "python3-fastapi/uvicorn missing — skip audio-player-backend.service"
    fi
}

apply_nginx() {
    apply_tmpfs_log_dirs
    local src="${SCRIPT_DIR}/config/nginx/audio-player.conf"
    local dst="/etc/nginx/sites-available/audio-player"
    [[ -f "$src" ]] || { warn "nginx config missing — skip"; return 0; }
    if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
        :
    else
        run cp "$src" "$dst"
    fi
    run ln -sf /etc/nginx/sites-available/audio-player /etc/nginx/sites-enabled/audio-player
    run rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true
    if command -v nginx >/dev/null 2>&1; then
        run nginx -t
        run systemctl enable nginx.service 2>/dev/null || true
        run systemctl reload nginx.service 2>/dev/null || run systemctl restart nginx.service 2>/dev/null || true
        # Optional web login: make nginx match auth.enabled in player.yaml.
        if [[ -x "${INSTALL_DIR}/scripts/web_auth.sh" ]]; then
            run "${INSTALL_DIR}/scripts/web_auth.sh" apply || warn "web login: could not apply (see above)"
        fi
        log "nginx configured for Safydo UI"
    else
        warn "nginx not installed — UI not served until nginx is available"
    fi
}

enable_phase4_services() {
    if [[ -d "${INSTALL_DIR}/frontend/dist" && -f "${INSTALL_DIR}/frontend/dist/index.html" ]]; then
        apply_nginx
        log "Phase 4 UI dist present"
    else
        warn "frontend/dist missing — build on dev: cd frontend && npm run build"
    fi
}

enable_phase6_services() {
    run chmod +x "${INSTALL_DIR}/scripts/usb_automount.sh" 2>/dev/null || true
    run chmod +x "${INSTALL_DIR}/scripts/phase6_smoke.sh" 2>/dev/null || true
    run chmod +x "${INSTALL_DIR}/scripts/bt_codec_prime.sh" 2>/dev/null || true
    run systemctl enable audio-player.path 2>/dev/null || true
    run systemctl enable audio-player-usb-rescan.service 2>/dev/null || true
    run mkdir -p /media/usb
    run chmod 755 /media/usb
    log "Phase 6 USB automount path + rescan enabled"
}

enable_phase5_services() {
    run chmod +x "${INSTALL_DIR}/scripts/network_priority.sh" 2>/dev/null || true
    run chmod +x "${INSTALL_DIR}/scripts/captive_portal.sh" 2>/dev/null || true
    run chmod +x "${INSTALL_DIR}/scripts/network_ready.sh" 2>/dev/null || true
    run systemctl enable audio-player-network-ready.service 2>/dev/null || true
    log "Post-boot WiFi ready service enabled (audio-player-network-ready)"

    # Two boards on one LAN would otherwise both answer to safydo.local and let
    # avahi break the tie with a counter, so which box is which changes with
    # boot order. This claims a name derived from the box's own last IP octet.
    run systemctl enable audio-player-hostname-conflict.service 2>/dev/null || true
    log "mDNS conflict guard enabled (audio-player-hostname-conflict)"

    # On by default (network.captive_portal_enabled): on a fresh board with no
    # known network this AP is the only way in short of a serial cable. It can
    # only act where a wireless adapter exists, so an ethernet-only box never
    # raises one, and it stands down as soon as a real uplink appears.
    run systemctl enable audio-player-captive-watch.timer 2>/dev/null || true
    run systemctl start audio-player-captive-watch.timer 2>/dev/null || true
    log "Setup-AP fallback enabled (captive_portal_enabled + a wireless adapter)"
    local eth_pri
    eth_pri="$(ethernet_priority_enabled)"
    if [[ "${eth_pri}" == "true" ]] && command -v ip >/dev/null 2>&1; then
        run systemctl enable network-priority.timer 2>/dev/null || true
        run systemctl start network-priority.timer 2>/dev/null || true
        run systemctl start network-priority.service 2>/dev/null || true
        log "Ethernet priority timer enabled (network.ethernet_priority=true)"
    else
        run systemctl disable --now network-priority.timer 2>/dev/null || true
        log "Ethernet priority timer disabled (set network.ethernet_priority: true when USB LAN works)"
    fi
    # First-boot captive: start if no saved WiFi networks
    if [[ -x "${INSTALL_DIR}/scripts/captive_portal.sh" ]]; then
        if ! grep -qE '^[[:space:]]*ssid=' /etc/wpa_supplicant/wpa_supplicant.conf 2>/dev/null; then
            local cap_flag
            cap_flag="$(python3 -c "
import yaml
from pathlib import Path
p = Path('${INSTALL_DIR}/config/player.yaml')
d = yaml.safe_load(p.read_text()) or {}
print(str((d.get('network') or {}).get('captive_portal_enabled', False)).lower())
" 2>/dev/null || echo false)"
            if [[ "${cap_flag}" == "true" ]]; then
                warn "No WiFi saved — run: sudo ${INSTALL_DIR}/scripts/captive_portal.sh start"
            fi
        fi
    fi
}

unit_exists() { systemctl list-unit-files "$1" &>/dev/null; }

# Disable a unit only if it is installed and not on the keep list. Disable, not
# mask: everything here must stay one `systemctl enable` away from coming back,
# and masking would also break D-Bus activation for the units that use it
# (wpa_supplicant is started on demand by NetworkManager that way).
disable_unit() {
    local svc="$1" why="${2:-}"
    unit_exists "$svc" || return 0
    if [[ " ${KEEP_SERVICES} " == *" ${svc} "* ]]; then
        log "  keep    ${svc} (SAFYDO_KEEP_SERVICES)"
        return 0
    fi
    run systemctl disable --now "$svc" >/dev/null 2>&1 || true
    log "  disable ${svc}${why:+  — ${why}}"
}

# Printer support. Raspberry Pi OS Lite ships none of this, so on a stock image
# every branch below is a no-op — it is here so the same installer stays honest
# on a Desktop image or an SD card that has been used for something else.
# Masked rather than removed by default (reversible, and a purge can drag half
# of GTK out with it); PURGE_PRINTING=1 actually removes the packages.
disable_printing() {
    local svc pkg found=()
    for pkg in cups cups-daemon cups-browsed cups-common ipp-usb \
               system-config-printer sane-utils colord; do
        dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "^install ok installed" \
            && found+=("$pkg")
    done

    for svc in cups.service cups.socket cups.path cups-browsed.service ipp-usb.service; do
        unit_exists "$svc" || continue
        run systemctl disable --now "$svc" >/dev/null 2>&1 || true
        run systemctl mask "$svc" >/dev/null 2>&1 || true
        log "  mask    ${svc}"
    done

    # Avahi advertises printers from drop-in service files; nothing on Lite puts
    # one there, but a leftover keeps the box announcing a printer it hasn't got.
    for svc in /etc/avahi/services/cups.service /etc/avahi/services/printer.service; do
        [[ -f "$svc" ]] && { run rm -f "$svc"; log "  remove  ${svc}"; }
    done

    if [[ ${#found[@]} -eq 0 ]]; then
        log "Printing: no packages installed (Lite image) — services masked defensively"
    elif [[ "${PURGE_PRINTING:-}" == "1" ]] && ! $SKIP_APT; then
        run apt-get -y -qq --purge remove "${found[@]}" || warn "printing purge failed"
        log "Printing: purged ${found[*]}"
    else
        log "Printing: masked but still installed (${found[*]}) — PURGE_PRINTING=1 to remove"
    fi
}

# Trim everything a headless audio appliance never uses. Two goals: keep the
# CPU free during A2DP playback (a background apt run is audible as dropouts on
# a 512 MB board) and stop needless SD writes, which is also what keeps the card
# alive across the sudden power cuts this thing is built for.
disable_heavy_services() {
    local svc

    log "Slimming service profile:"

    disable_unit triggerhappy.service "GPIO hotkey daemon, unused"

    # The dnsmasq package enables a system-wide resolver on :53. This box does
    # not need one, and it actively breaks the captive portal: our own dnsmasq
    # serves DHCP to the AP clients and cannot bind while the stock one holds
    # the port, so the SSID appears but nobody ever gets a lease. hostapd and
    # the gadget DHCP run dnsmasq directly from their own units with explicit
    # configs, so nothing here depends on the packaged service.
    disable_unit dnsmasq.service "system-wide resolver; blocks the captive portal DHCP"

    # ALSA direct — no sound server anywhere in this stack.
    for svc in pipewire.service pipewire-pulse.service pulseaudio.service; do
        disable_unit "$svc" "sound server, ALSA is direct"
    done

    # Unattended apt is the single worst offender: it wakes up mid-playback,
    # pins the CPU and writes hundreds of MB to the card. install.sh does the
    # upgrading now, deliberately and while nothing is playing.
    disable_unit apt-daily.timer          "background apt during playback"
    disable_unit apt-daily-upgrade.timer  "background apt during playback"
    disable_unit unattended-upgrades.service "background apt during playback"

    # Pure SD churn on an appliance that has no man pages worth indexing and a
    # package DB that install.sh can rebuild from scratch anyway.
    disable_unit man-db.timer        "man cache rebuild, SD writes"
    disable_unit dpkg-db-backup.timer "dpkg DB copy, SD writes"

    # e2scrub only does anything on LVM volumes; the Pi roots on a plain
    # partition, so both units are dead weight at every boot.
    disable_unit e2scrub_all.timer   "LVM-only, no LVM here"
    disable_unit e2scrub_reap.service "LVM-only, no LVM here"

    disable_unit systemd-pstore.service "no pstore backend on Pi"
    disable_unit ModemManager.service   "no cellular modem"

    # mpris-proxy is a *user* unit (bluez ships it, enabled for every login):
    # it bridges Bluetooth media controls to a desktop's MPRIS. A headless
    # player has no desktop, and each SSH login started it and each logout
    # stopped it — registering and dropping an AVRCP player on the speaker's
    # link every time, extra work for a BT controller that wedges under load
    # (docs/BUILD.md). --global: the per-user default; reversible with enable.
    if [[ -f /usr/lib/systemd/user/mpris-proxy.service ]] && [[ ! " ${SAFYDO_KEEP_SERVICES:-} " =~ " mpris-proxy.service " ]]; then
        if systemctl --global is-enabled mpris-proxy.service >/dev/null 2>&1; then
            run systemctl --global disable mpris-proxy.service
            log "  disabled mpris-proxy.service (user unit) — desktop MPRIS bridge, no desktop here"
        fi
    fi

    # nfs-common (for NAS shares in Files) enables two daemons at boot that sit
    # idle on a box with no NFS mount — 3.6 MB measured on the 3 A+.
    # blkmapd serves only pNFS block layouts, which a read-only music share
    # never uses. rpcbind is needed only by NFSv3 mounts: the service is
    # disabled but rpcbind.socket stays enabled, so systemd starts it on demand
    # the moment a v3 mount asks. NFSv4 and SMB need neither.
    disable_unit nfs-blkmap.service "pNFS block layouts only"
    disable_unit rpcbind.service     "socket-activated on demand for NFSv3"

    # rpi-eeprom lives in SPI flash on the Pi 4/5 only. On a Zero 2W or 3 A+ the
    # bootloader is in the SoC ROM and this unit exits doing nothing.
    case "$(platform_board)" in
        zero2w|pi3aplus) disable_unit rpi-eeprom-update.service "Pi 4/5 only" ;;
    esac

    # cloud-init runs the Trixie image's first-boot setup (it is what applied
    # userconf/network config). Once that has finished it only costs boot time,
    # so it goes — but never before boot-finished exists, or the next boot comes
    # up unconfigured.
    if [[ -e /var/lib/cloud/instance/boot-finished ]]; then
        for svc in cloud-init-main.service cloud-init-local.service \
                   cloud-init-network.service cloud-config.service \
                   cloud-final.service cloud-init-hotplugd.socket; do
            disable_unit "$svc" "first boot already done"
        done
    elif unit_exists cloud-init-main.service; then
        log "  keep    cloud-init (first boot has not finished yet)"
    fi

    log "Lightweight service profile applied"
}

# Run a helper from scripts/_common.sh without importing it.
#
# A separate bash process, not a subshell: install.sh marks SCRIPT_DIR and
# INSTALL_DIR readonly, _common.sh assigns both at load, and under its
# `set -e` that failed assignment aborts the source — leaving the caller with an
# empty string instead of an answer. Sourcing also replaces install.sh's own
# log/warn/die with _common.sh's. A clean process has neither problem.
# Echoes nothing if the helper is unavailable; callers treat empty as "unknown".
common_query() {
    bash -c 'source "$1/scripts/_common.sh" >/dev/null 2>&1 || exit 1; shift; "$@"' \
        safydo-query "$SCRIPT_DIR" "$@" 2>/dev/null || true
}

# The UI reads hostname/mdns_name from player.yaml, not /etc/hostname, so
# leaving them stale made the Network panel advertise an address that may now
# belong to a different board — newly visible since avahi stopped overriding
# the system hostname. Runs AFTER deploy_project because that is when the
# config file exists, and via common_query because install.sh cannot source
# _common.sh (both mark SCRIPT_DIR/INSTALL_DIR readonly).
sync_identity_config() {
    if $DRY_RUN; then
        log "[dry-run] set config hostname/mdns_name to ${HOSTNAME}"
        return 0
    fi
    common_query cfg_set "hostname" "$HOSTNAME"
    common_query cfg_set "mdns_name" "${HOSTNAME}.local"
    # Mount ownership must not depend on who happens to invoke usb_automount.
    common_query cfg_set "paths.owner" "${SUDO_USER:-$(id -un)}"
    log "Config identity synced: ${HOSTNAME} / ${HOSTNAME}.local"
}

# Onboard WiFi is the one "unwanted" thing that can lock you out of the box, so
# it is the one thing here that refuses to act on a guess. Both boards share a
# single 2.4 GHz radio between WiFi and BT, and A2DP is what suffers — but the
# radio is also how a lot of people are connected while running this.
#
# off   — the default. Disable on every board and OS, unless that would leave no
#         way back in (see the guard below), in which case defer and say so.
# auto  — the older, more timid rule: disable only when a USB uplink is already
#         carrying the box. Kept because someone may be scripting against it.
# keep  — never touch it.
#
# The guard, in both modes, is about links that exist *now*, not links that
# could exist: an alternate uplink must be present, and we refuse to drop the
# radio the default route is currently riding unless a USB uplink is plugged in
# to take over. A staged USB gadget tether is a fallback for the first case
# (something is reachable after the reboot) but never for the second — it needs
# a person and a cable at the board, which is no comfort to a remote session.
apply_onboard_wifi_policy() {
    local cfg wlan uplink route_if gadget=false reason=""

    if [[ "$ONBOARD_WIFI" == "keep" ]]; then
        log "Onboard WiFi: left enabled (--keep-onboard-wifi)"
        return 0
    fi

    cfg="$(boot_config_path)"
    if [[ -n "$cfg" ]] && grep -q "SAFYDO onboard-wifi-disable begin" "$cfg" 2>/dev/null; then
        log "Onboard WiFi: already disabled in ${cfg}"
        return 0
    fi

    wlan="$(common_query onboard_wlan_iface)"
    if [[ -z "$wlan" ]]; then
        log "Onboard WiFi: no onboard radio present — nothing to disable"
        return 0
    fi

    uplink="$(common_query usb_uplink_iface)"
    route_if="$(common_query default_route_iface)"
    board_supports_gadget && systemctl is-enabled usb-gadget.service >/dev/null 2>&1 && gadget=true

    # Guard 1 — nothing else can carry this box. Disabling here also kills the
    # setup AP as a recovery path (that AP needs the very radio being switched
    # off), so all that is left is a serial cable or the SD card in a PC.
    if [[ -z "$uplink" ]] && ! $gadget; then
        reason="no USB uplink with carrier+IP, and no gadget dev link to fall back on"
    # Guard 2 — the onboard radio is carrying the default route and nothing is
    # plugged in to take over. On a gadget board the tether would survive, but
    # it needs someone standing at the board with a cable.
    elif [[ "$route_if" == "$wlan" && -z "$uplink" ]]; then
        reason="the default route runs over ${wlan} and no USB uplink is plugged in to take over"
    # The conservative mode additionally declines while the radio still owns the
    # route, even though a dongle is present and would inherit it after reboot.
    elif [[ "$ONBOARD_WIFI" == "auto" && "$route_if" == "$wlan" ]]; then
        reason="ONBOARD_WIFI=auto and the default route still runs over ${wlan} (uplink ${uplink} would take over, but auto will not assume it)"
    fi

    if [[ -n "$reason" ]]; then
        ONBOARD_WIFI_DEFERRED="$reason"
        warn "=================================================================="
        warn "ONBOARD WIFI LEFT ON — ${reason}"
        warn "It shares the 2.4 GHz radio with Bluetooth: expect A2DP dropouts"
        warn "until it is off. Plug a USB uplink, confirm you can reach the box"
        warn "over it, then run:"
        warn "  sudo ${INSTALL_DIR}/scripts/wifi_onboard.sh disable --confirm"
        warn "  sudo reboot"
        warn "=================================================================="
        return 0
    fi

    log "Onboard WiFi: uplink=${uplink:-none} gadget=${gadget} route=${route_if:-none} — disabling"

    # wifi_onboard.sh owns the config.txt edit and re-checks the guard itself.
    if run_sh "${SCRIPT_DIR}/scripts/wifi_onboard.sh" disable --confirm; then
        REBOOT_REQUIRED=true
        log "Onboard WiFi disabled — takes effect on the next reboot"
    else
        ONBOARD_WIFI_DEFERRED="wifi_onboard.sh refused (see its output above)"
        warn "=================================================================="
        warn "ONBOARD WIFI LEFT ON — wifi_onboard.sh refused to disable it"
        warn "Its own lockout guard disagreed with ours; read its message above."
        warn "=================================================================="
    fi
}

# Make sure the wedge-recovery switch exists and say what this board needs from
# it. The switch itself is deliberately NOT decided here.
#
# On boards where unbinding the driver cannot power-cycle the BT chip (BT_ON on
# the VideoCore expander — Pi 3 A+, Pi 4) a reboot is the only recovery from a
# wedged controller, and it is tempting to just turn that on. But rebooting the
# machine on the watchdog's own judgement is the user's call — the project spec
# says so in as many words — and an installer that quietly enables it takes that call
# away from someone who may be listening to music at the time. So: the key is
# created with a safe default, the recommendation is printed here and repeated
# in the summary, and the switch lives in the web UI (Settings → Bluetooth)
# where requirement 12 says every control belongs.
#
# SAFYDO_WEDGE_REBOOT=true/false still decides it at install time, for anyone
# provisioning boards unattended.
apply_bt_wedge_policy() {
    local viable current want cfg="${INSTALL_DIR}/config/player.yaml"
    [[ -f "$cfg" ]] || return 0

    viable="$(common_query bt_rebind_viable_str)"
    case "$viable" in
        no)  BT_WEDGE_NOTE="a rebind cannot recover this board's controller — only a reboot can"
             BT_WEDGE_ADVISE=true ;;
        yes) BT_WEDGE_NOTE="a rebind recovers this board's controller; no reboot needed" ;;
        *)   BT_WEDGE_NOTE="cannot tell whether a rebind recovers this board (install gpiod for the precise probe)"
             BT_WEDGE_ADVISE=true ;;
    esac

    current="$(common_query cfg_get bluetooth.wedge_reboot)"
    current="${current//\'/}"
    current="${current,,}"

    if [[ -n "${SAFYDO_WEDGE_REBOOT:-}" ]]; then
        # Normalised, so an unattended provisioner passing 1/yes/on cannot write
        # a value the watchdog would read as "off".
        case "${SAFYDO_WEDGE_REBOOT,,}" in
            true|1|yes|on) want=true ;;
            *)             want=false ;;
        esac
    elif [[ "$current" == "true" || "$current" == "false" ]]; then
        # Someone — a person over SSH, or the web UI — has already answered
        # this. Re-deriving it on every run would undo their answer on the next
        # deploy, silently, which is exactly what a preserved config section is
        # supposed to prevent.
        log "BT wedge policy: reboot escalation is ${current} (left as set); ${BT_WEDGE_NOTE}"
        BT_WEDGE_STATE="$current"
        return 0
    else
        want=false   # never on unless someone said so
    fi

    BT_WEDGE_STATE="$want"
    if [[ "$current" == "$want" ]]; then
        log "BT wedge policy: reboot escalation ${want}; ${BT_WEDGE_NOTE}"
        return 0
    fi
    if $DRY_RUN; then
        log "[dry-run] would set bluetooth.wedge_reboot=${want} (${BT_WEDGE_NOTE})"
        return 0
    fi
    if run_sh python3 "${INSTALL_DIR}/scripts/_yaml_edit.py" --config "$cfg" \
            set bluetooth.wedge_reboot "$want"; then
        log "BT wedge policy: reboot escalation ${want}; ${BT_WEDGE_NOTE}"
    else
        warn "BT wedge policy: could not write bluetooth.wedge_reboot"
        BT_WEDGE_STATE="$current"
    fi
}

# One line for the summary: what this board can recover from by itself.
bt_wedge_summary() {
    local state="${BT_WEDGE_STATE:-unknown}"
    if $BT_WEDGE_ADVISE && [[ "$state" != "true" ]]; then
        echo "reboot escalation OFF — ${BT_WEDGE_NOTE}."
        echo "                 Turn it on in the web UI (Settings → Bluetooth) to let the box"
        echo "                 recover unattended, or reboot by hand when audio goes silent."
    else
        echo "reboot escalation ${state}; ${BT_WEDGE_NOTE:-state unknown}"
    fi
}

enable_core_services() {
    run systemctl enable ssh.service 2>/dev/null || run systemctl enable sshd.service 2>/dev/null || true
    run systemctl enable avahi-daemon.service
}

deploy_project() {
    run mkdir -p "$INSTALL_DIR" "$STATE_DIR" "$RUN_DIR"
    run mkdir -p "${MARKER_DIR}"

    if $DRY_RUN; then
        log "[dry-run] rsync ${SCRIPT_DIR}/ -> ${INSTALL_DIR}/"
        return 0
    fi

    # Preserve Pi-local runtime edits (e.g. bluetooth.primary_mac)
    local player_cfg_backup=""
    if [[ -f "${INSTALL_DIR}/config/player.yaml" ]]; then
        player_cfg_backup="$(mktemp)"
        cp "${INSTALL_DIR}/config/player.yaml" "${player_cfg_backup}"
    fi

    rsync -a --delete \
        --exclude '.git' \
        --exclude 'frontend/node_modules' \
        --exclude 'frontend/dist' \
        --exclude '.venv' \
        --exclude 'config/player.yaml' \
        --exclude 'config/streams.json' \
        --exclude 'config/eq_presets.json' \
        "${SCRIPT_DIR}/" "${INSTALL_DIR}/"

    # Stations and EQ presets are edited from the web UI, so they belong to the
    # user once they exist. The repo's copies only seed a fresh install.
    local seed
    for seed in streams.json eq_presets.json; do
        [[ -f "${INSTALL_DIR}/config/${seed}" ]] || cp "${SCRIPT_DIR}/config/${seed}" "${INSTALL_DIR}/config/${seed}"
    done

    # frontend/dist is excluded above so a source tree *without* a build (fresh
    # clone, or a deploy that didn't rebuild) can never --delete a working UI out
    # from under nginx. But when the source does carry a build, install.sh has to
    # publish it: otherwise `git clone && sudo ./install.sh` finishes with nginx
    # serving nothing, which is exactly how the first 3 A+ install ended.
    if [[ -n "$(ls -A "${SCRIPT_DIR}/frontend/dist" 2>/dev/null)" ]]; then
        mkdir -p "${INSTALL_DIR}/frontend/dist"
        rsync -a --delete "${SCRIPT_DIR}/frontend/dist/" "${INSTALL_DIR}/frontend/dist/"
        log "Deployed built UI from ${SCRIPT_DIR}/frontend/dist"
    else
        log "No frontend/dist in source tree — leaving any deployed UI untouched"
        log "  (build it on the dev box: cd frontend && npm run build)"
    fi

    if [[ -n "${player_cfg_backup}" && -f "${player_cfg_backup}" ]]; then
        if [[ -x "${SCRIPT_DIR}/scripts/merge_player_config.py" ]] || [[ -f "${SCRIPT_DIR}/scripts/merge_player_config.py" ]]; then
            python3 "${SCRIPT_DIR}/scripts/merge_player_config.py" \
                --template "${SCRIPT_DIR}/config/player.yaml" \
                --live "${player_cfg_backup}" \
                --out "${INSTALL_DIR}/config/player.yaml"
        else
            cp "${player_cfg_backup}" "${INSTALL_DIR}/config/player.yaml"
        fi
        rm -f "${player_cfg_backup}"
    elif [[ ! -f "${INSTALL_DIR}/config/player.yaml" ]]; then
        cp "${SCRIPT_DIR}/config/player.yaml" "${INSTALL_DIR}/config/player.yaml"
    fi

    chmod +x "${INSTALL_DIR}/install.sh"
    chmod +x "${INSTALL_DIR}/scripts/"*.sh 2>/dev/null || true
    chmod +x "${INSTALL_DIR}/scripts/"*.py 2>/dev/null || true
    # Convenience launcher for the SSH terminal UI (scripts/tui.py).
    ln -sf "${INSTALL_DIR}/scripts/tui.py" /usr/local/bin/safydo-tui 2>/dev/null || true
    # No automatic import_stations.py here: it rebuilt streams.json from stations.json
    # on every install, discarding whatever the user had edited in the web UI.
    log "Project deployed to ${INSTALL_DIR}"
}

deploy_systemd_units() {
    local unit src
    for src in "${SCRIPT_DIR}"/systemd/*; do
        [[ -f "$src" ]] || continue
        unit="$(basename "$src")"
        if [[ -f "/etc/systemd/system/${unit}" ]] && cmp -s "$src" "/etc/systemd/system/${unit}"; then
            continue
        fi
        run cp "$src" "/etc/systemd/system/${unit}"
    done
    run systemctl daemon-reload
    # Backend and network priority enabled in later phases
    log "Systemd units deployed (not all enabled until later phases)"
}

deploy_udev_rules() {
    local rule src
    for src in "${SCRIPT_DIR}"/udev/*; do
        [[ -f "$src" ]] || continue
        rule="$(basename "$src")"
        run cp "$src" "/etc/udev/rules.d/${rule}"
    done
    run udevadm control --reload-rules 2>/dev/null || true
}

# Web links (backend/services/links.py, mpv's ytdl_hook). The Debian yt-dlp
# falls behind YouTube within weeks, so yt-dlp lives in its own venv at
# /opt/yt-dlp with curl_cffi (browser impersonation: Rumble's API answers 403
# to a plain client), linked as /usr/local/bin/yt-dlp (mpv_daemon.sh pins
# that path). audio-player-ytdlp-update.timer upgrades it weekly via
# scripts/ytdlp_update.sh. If the venv cannot be built (no internet, pip
# failure) the official zipapp is the fallback: everything but Rumble works.
# YTDLP=0 skips all of this.
#
# YouTube may also need a JavaScript runtime for its player challenge. Node.js
# is the one Raspbian ships for armhf; yt-dlp wants Node 20+, which Trixie has
# (20.19) and Bookworm does not (18.x).
YTDLP_VENV=/opt/yt-dlp
YTDLP_SPEC='yt-dlp[default,curl-cffi]'
install_ytdlp() {
    if [[ "${YTDLP:-1}" == "0" ]]; then
        log "Web links: yt-dlp skipped (YTDLP=0)"
        return 0
    fi
    local exe=/usr/local/bin/yt-dlp
    if [[ ! -x "${YTDLP_VENV}/bin/yt-dlp" ]]; then
        if run python3 -m venv "$YTDLP_VENV" \
                && run "${YTDLP_VENV}/bin/pip" install -q --disable-pip-version-check "$YTDLP_SPEC"; then
            log "Web links: yt-dlp venv built at ${YTDLP_VENV}"
        else
            warn "Web links: could not build the yt-dlp venv — falling back to the zipapp (Rumble will not work)"
            run rm -rf "$YTDLP_VENV"
        fi
    fi
    if [[ -x "${YTDLP_VENV}/bin/yt-dlp" ]]; then
        # Replaces an earlier zipapp in place; mpv and the backend keep the path.
        [[ "$(readlink -f "$exe" 2>/dev/null)" == "${YTDLP_VENV}/bin/yt-dlp" ]] \
            || run ln -sfn "${YTDLP_VENV}/bin/yt-dlp" "$exe"
    elif [[ ! -x "$exe" ]]; then
        local tmp
        tmp="$(mktemp)"
        if run curl -fsSL --max-time 120 -o "$tmp" \
                https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp; then
            run install -m 755 "$tmp" "$exe"
        else
            warn "Web links: could not download yt-dlp (no internet?) — links stay unplayable until the next install"
        fi
        rm -f "$tmp"
    fi
    if ! $SKIP_APT && ! command -v node >/dev/null 2>&1; then
        run apt-get install -y -qq nodejs || warn "Web links: nodejs unavailable — YouTube may not play"
    fi
    local node_major=0
    if command -v node >/dev/null 2>&1; then
        node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
    fi
    if (( node_major >= 20 )); then
        if ! grep -qs -- '--js-runtimes node' /etc/yt-dlp.conf; then
            run_sh "printf '%s\n' '# SAFYDO: JS runtime for YouTube (install.sh install_ytdlp)' '--js-runtimes node' >> /etc/yt-dlp.conf"
        fi
    else
        log "Web links: no Node.js 20+ (have ${node_major}) — fine unless YouTube starts failing"
    fi
    if [[ -x "$exe" ]] && unit_exists audio-player-ytdlp-update.timer; then
        # Settings -> Modules -> Web links off also stops the weekly upgrade
        # (backend/main.py put_config); a re-install must not switch it back on.
        if [[ "$(common_query cfg_get modules.links)" =~ ^([Ff]alse|0|no)$ ]]; then
            run systemctl disable --now audio-player-ytdlp-update.timer
            log "Web links: module off in Settings — weekly yt-dlp upgrade left disabled"
        else
            run systemctl enable --now audio-player-ytdlp-update.timer
        fi
    fi
    [[ -x "$exe" ]] && log "Web links: yt-dlp $("$exe" --version 2>/dev/null || echo '?') ($(readlink -f "$exe"))"
    return 0
}

create_state_dirs() {
    run mkdir -p "$STATE_DIR" "$RUN_DIR"
    run chmod 755 "$STATE_DIR" "$RUN_DIR"
}

mark_phase0() {
    if $DRY_RUN; then
        log "[dry-run] mark phase0 complete"
        return 0
    fi
    echo "$(date -Iseconds)" > "$PHASE0_MARKER"
    log "Phase 0 marker written: ${PHASE0_MARKER}"
}

mark_phase1() {
    if $DRY_RUN; then
        log "[dry-run] mark phase1 complete"
        return 0
    fi
    echo "$(date -Iseconds)" > "$PHASE1_MARKER"
    log "Phase 1 marker written: ${PHASE1_MARKER}"
}

mark_phase2() {
    if $DRY_RUN; then
        log "[dry-run] mark phase2 complete"
        return 0
    fi
    if command -v mpv >/dev/null 2>&1; then
        echo "$(date -Iseconds)" > "$PHASE2_MARKER"
        log "Phase 2 marker written: ${PHASE2_MARKER}"
    else
        warn "Phase 2 marker skipped — mpv not installed"
    fi
}

mark_phase3() {
    if $DRY_RUN; then
        log "[dry-run] mark phase3 complete"
        return 0
    fi
    if python3 -c "import fastapi, uvicorn" 2>/dev/null; then
        echo "$(date -Iseconds)" > "$PHASE3_MARKER"
        log "Phase 3 marker written: ${PHASE3_MARKER}"
    else
        warn "Phase 3 marker skipped — FastAPI not installed"
    fi
}

mark_phase4() {
    if $DRY_RUN; then
        log "[dry-run] mark phase4 complete"
        return 0
    fi
    if [[ -f "${INSTALL_DIR}/frontend/dist/index.html" ]] && command -v nginx >/dev/null 2>&1; then
        echo "$(date -Iseconds)" > "$PHASE4_MARKER"
        log "Phase 4 marker written: ${PHASE4_MARKER}"
    else
        warn "Phase 4 marker skipped — build frontend/dist or install nginx"
    fi
}

mark_phase5() {
    if $DRY_RUN; then
        log "[dry-run] mark phase5 complete"
        return 0
    fi
    if [[ -x "${INSTALL_DIR}/scripts/network_priority.sh" && -x "${INSTALL_DIR}/scripts/captive_portal.sh" ]]; then
        echo "$(date -Iseconds)" > "$PHASE5_MARKER"
        log "Phase 5 marker written: ${PHASE5_MARKER}"
    else
        warn "Phase 5 marker skipped — network scripts missing"
    fi
}

# The installer is deliberately forgiving — one missing optional package must
# not abort a 25-minute run. But "finished with warnings" and "working" are not
# the same claim, and exiting 0 on a box with no mpv and no backend is exactly
# how a broken install gets mistaken for a good one. Fail loudly on the pieces
# that are not optional.
verify_install() {
    if $DRY_RUN; then
        log "Skipping install verification (--dry-run)"
        return 0
    fi
    local missing=()
    command -v mpv   >/dev/null 2>&1 || missing+=("mpv[player]")
    command -v nginx >/dev/null 2>&1 || missing+=("nginx[web-ui-server]")
    python3 -c "import fastapi, uvicorn" >/dev/null 2>&1 || missing+=("python3-fastapi/uvicorn[backend]")
    [[ -f "${INSTALL_DIR}/frontend/dist/index.html" ]] || missing+=("frontend/dist[built-ui]")
    command -v bluealsa >/dev/null 2>&1 || [[ -x /usr/local/bin/bluealsa ]] || missing+=("bluealsa[bt-audio]")

    if [[ ${#missing[@]} -eq 0 ]]; then
        log "Install verified — player, backend, UI and BT audio all present"
        return 0
    fi
    warn "=============================================="
    warn "INSTALL INCOMPLETE — the web UI will not answer"
    warn "Missing: ${missing[*]}"
    warn "Check the apt warnings above, then re-run:"
    warn "  sudo ${INSTALL_DIR}/install.sh"
    warn "=============================================="
    return 1
}

onboard_wifi_summary() {
    local cfg
    if $REBOOT_REQUIRED; then
        $DRY_RUN && { echo "would be disabled (dry-run made no change)"; return; }
        echo "DISABLED — reboot to apply (BT then owns 2.4 GHz)"
        return
    fi
    cfg="$(boot_config_path)"
    if [[ -n "$cfg" ]] && grep -q "SAFYDO onboard-wifi-disable begin" "$cfg" 2>/dev/null; then
        echo "disabled (BT owns the 2.4 GHz radio)"
    elif [[ -z "$(common_query onboard_wlan_iface)" ]]; then
        echo "no onboard radio on this board"
    elif [[ -n "$ONBOARD_WIFI_DEFERRED" ]]; then
        echo "STILL ON — ${ONBOARD_WIFI_DEFERRED}"
    else
        echo "ENABLED — shares 2.4 GHz with BT; disable with --disable-onboard-wifi"
    fi
}

# Only the Zero 2W has a gadget-capable port. Printing the tether address on a
# board that cannot have one sends someone chasing a link that does not exist,
# at the moment they are most likely to be locked out.
dev_link_summary() {
    if board_supports_gadget; then
        cat <<EOF
USB dev link (after reboot): tether USB data port to PC ->
   ssh ${SUDO_USER:-$(id -un)}@10.55.0.1   or   screen /dev/ttyACM0 115200
EOF
    else
        cat <<'EOF'
Console fallback: GPIO serial on pins 6/8/10 (USB-TTL adapter, 115200)
   — this board has no USB gadget dev link
EOF
    fi
}

print_summary() {
    cat <<EOF

========================================
 RPI Audio Player — install complete
========================================
 Platform    : $(platform_describe)
 Install dir : ${INSTALL_DIR}
 mDNS        : ${HOSTNAME}.local
 SSH         : ssh ${SUDO_USER:-$(id -un)}@${HOSTNAME}.local

 Phase 0 checks:
   sysctl net.core.rmem_max
   systemctl status avahi-daemon
   hostname

 Phase 1 — Bluetooth audio:
   sudo ${INSTALL_DIR}/scripts/bt_pair.sh scan
   sudo ${INSTALL_DIR}/scripts/bt_pair.sh pair <MAC>
   sudo ${INSTALL_DIR}/scripts/play_test.sh
   cat /var/lib/audio-player/bt_status.json

 Phase 2 — Core player:
   sudo systemctl start audio-player-mpv
   sudo ${INSTALL_DIR}/scripts/playerctl.py status
   sudo ${INSTALL_DIR}/scripts/playerctl.py play-stream jazz24
   sudo ${INSTALL_DIR}/scripts/import_stations.py
   sudo ${INSTALL_DIR}/scripts/stream_test.sh

 Phase 3 — Backend API:
   curl -s http://127.0.0.1:8000/api/health
   curl -s http://127.0.0.1:8000/api/streams?favorites=true

 Phase 4 — Web UI:
   http://${HOSTNAME}.local/
   (build: cd frontend && npm run build, then redeploy)

 Phase 5 — Network:
   sudo ${INSTALL_DIR}/scripts/network_priority.sh
   sudo ${INSTALL_DIR}/scripts/captive_portal.sh start

 Reboot is OK after install (WiFi fixes: NM-only, network_ready service).
   sudo reboot
   # then: curl -sI https://google.com | head -1

 $(dev_link_summary)
 Onboard WiFi : $(onboard_wifi_summary)
   status / undo: sudo ${INSTALL_DIR}/scripts/wifi_onboard.sh status|enable
 BT wedge     : $(bt_wedge_summary)
   check now  : sudo ${INSTALL_DIR}/scripts/bt_watchdog.sh --dry-run

 Optional boot tuning (test after reboot): APPLY_OVERCLOCK=1

 Docs: ${INSTALL_DIR}/docs/phase0.md
        ${INSTALL_DIR}/docs/phase1.md
        ${INSTALL_DIR}/docs/phase2.md
        ${INSTALL_DIR}/docs/phase3.md
        ${INSTALL_DIR}/docs/phase4.md
        ${INSTALL_DIR}/docs/phase5.md
========================================
EOF
}

# The banner from apply_onboard_wifi_policy() scrolls past 20 minutes of apt
# output. Repeat it as the last thing on screen, where it will actually be read.
onboard_wifi_reminder() {
    [[ -n "$ONBOARD_WIFI_DEFERRED" ]] || return 0
    warn ""
    warn "REMINDER: onboard WiFi is still ON (${ONBOARD_WIFI_DEFERRED})."
    warn "It costs Bluetooth airtime. Fix it with:"
    warn "  sudo ${INSTALL_DIR}/scripts/wifi_onboard.sh disable --confirm && sudo reboot"
}

post_install_audio_stack() {
    # After bluealsa restart, mpv may start before BT is up — reconnect + restart mpv.
    local mac
    mac="$(python3 -c "
import yaml
from pathlib import Path
p = Path('${INSTALL_DIR}/config/player.yaml')
if not p.exists():
    raise SystemExit(0)
d = yaml.safe_load(p.read_text()) or {}
print((d.get('bluetooth') or {}).get('primary_mac') or '')
" 2>/dev/null || true)"
    [[ -n "$mac" ]] || return 0
    if ! systemctl is-active bluetooth.service >/dev/null 2>&1; then
        return 0
    fi
    if [[ -x "${INSTALL_DIR}/scripts/bt_connect.sh" ]]; then
        run "${INSTALL_DIR}/scripts/bt_connect.sh" "$mac" 2>/dev/null || \
            warn "post-install BT connect failed — run bt_connect.sh manually"
    fi
    run systemctl restart audio-player-mpv.service 2>/dev/null || true
    log "Post-install audio stack: BT + mpv refreshed"
}

# Playback across a deploy. The backend's resume-on-boot fires when the install
# restarts it, but post_install_audio_stack restarts mpv after that, so a
# deploy used to end in silence. Record before anything restarts whether mpv
# was playing (not idle, not paused), and if so ask the backend to resume the
# saved source at the end. A user who had stopped or paused stays that way.
# Straight to uvicorn on loopback: no nginx, no web login.
BACKEND_LOCAL="http://127.0.0.1:8000"
WAS_PLAYING=false

record_playback_state() {
    $DRY_RUN && return 0
    command -v curl >/dev/null 2>&1 || return 0
    local status
    status="$(curl -fsS --max-time 4 "${BACKEND_LOCAL}/api/status" 2>/dev/null)" || return 0
    if python3 -c '
import json, sys
m = (json.loads(sys.argv[1]).get("player") or {}).get("mpv") or {}
sys.exit(0 if m.get("running") and m.get("idle") is False and not m.get("paused") else 1)
' "$status" 2>/dev/null; then
        WAS_PLAYING=true
        log "Playback in progress — will resume it after the install"
    fi
}

resume_playback() {
    $WAS_PLAYING || return 0
    local out
    # The backend waits up to 30 s for mpv's socket itself.
    out="$(curl -fsS --max-time 45 -X POST "${BACKEND_LOCAL}/api/play/resume" 2>/dev/null)" || {
        warn "could not resume playback after the install — press play in the UI"
        return 0
    }
    log "Resume after install: ${out}"
}

main() {
    parse_args "$@"
    require_root
    detect_pi

    log "Starting install (update_only=${UPDATE_ONLY}, skip_apt=${SKIP_APT}, dry_run=${DRY_RUN})"

    record_playback_state
    system_upgrade
    phase0_packages
    if ! $UPDATE_ONLY; then
        full_packages
    fi

    apply_boot_config
    apply_wifi_tuning
    configure_networkmanager_wifi
    apply_sysctl
    apply_swap_policy
    apply_avahi
    apply_fstab_tmpfs
    apply_tmpfs_log_dirs
    tune_root_filesystem
    disable_heavy_services
    disable_printing
    enable_core_services
    deploy_project
    sync_identity_config
    deploy_systemd_units
    deploy_udev_rules
    create_state_dirs
    install_ytdlp
    apply_bluetooth
    apply_bluealsa
    # Before phase 1: enable_phase1_services *starts* the watchdog timer, and
    # with OnBootSec elapsed it can fire immediately — against a config key this
    # has not written yet, moments after apply_bluetooth restarted bluetoothd.
    apply_bt_wedge_policy
    enable_phase1_services
    enable_phase2_services
    enable_phase3_services
    enable_phase4_services
    enable_phase5_services
    enable_phase6_services
    apply_usb_gadget
    post_install_audio_stack
    resume_playback
    # Last, and after apply_usb_gadget: on the Zero 2W the gadget dev link is
    # the fallback this is allowed to rely on, so it has to be enabled first.
    apply_onboard_wifi_policy
    mark_phase0
    mark_phase1
    mark_phase2
    mark_phase3
    mark_phase4
    mark_phase5
    print_summary
    local rc=0
    verify_install || rc=$?
    onboard_wifi_reminder
    return "$rc"
}

main "$@"
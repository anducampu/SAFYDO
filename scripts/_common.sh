#!/usr/bin/env bash
# Shared constants and helpers for player scripts
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/rpi-audio-player}"
CONFIG_DIR="${CONFIG_DIR:-${INSTALL_DIR}/config}"
STATE_DIR="${STATE_DIR:-/var/lib/audio-player}"
RUN_DIR="${RUN_DIR:-/run/audio-player}"
PLAYER_CONFIG="${PLAYER_CONFIG:-${CONFIG_DIR}/player.yaml}"
BT_STATUS_FILE="${BT_STATUS_FILE:-${STATE_DIR}/bt_status.json}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
YAML_EDIT="${YAML_EDIT:-${SCRIPT_DIR}/_yaml_edit.py}"

# Board/OS detection (platform_board, platform_os, board_* predicates).
# `if` rather than `[[ ]] &&`: a false test as the last command would abort the
# sourcing script under `set -e`.
if [[ -r "${SCRIPT_DIR}/platform.sh" ]]; then
    # shellcheck source=scripts/platform.sh
    source "${SCRIPT_DIR}/platform.sh"
fi

log()  { echo "[$(basename "$0")] $*" >&2; }
warn() { echo "[$(basename "$0")] WARNING: $*" >&2; }
die()  { echo "[$(basename "$0")] ERROR: $*" >&2; exit 1; }

require_cmd() {
    local cmd
    for cmd in "$@"; do
        command -v "$cmd" >/dev/null 2>&1 || die "missing command: ${cmd}"
    done
}

# Path to the active boot config.txt (Bookworm: /boot/firmware; legacy: /boot).
# Echoes the path, or nothing if neither exists (caller decides how to handle).
boot_config_path() {
    if [[ -f /boot/firmware/config.txt ]]; then
        echo /boot/firmware/config.txt
    elif [[ -f /boot/config.txt ]]; then
        echo /boot/config.txt
    fi
}

# First USB-attached uplink interface that is actually usable (carrier + IPv4).
# Matched by device path, not name: a USB ethernet dongle shows up as eth0 on a
# board with no built-in NIC, while WiFi dongles get MAC-derived wlx* names.
# The gadget dev link (usb0) is excluded — it is a PC tether, not an uplink.
# Echoes the interface name, or nothing. Never fails the caller.
usb_uplink_iface() {
    local path iface devpath
    for path in /sys/class/net/*; do
        iface="$(basename "$path")"
        [[ "$iface" == "lo" || "$iface" == usb* ]] && continue
        devpath="$(readlink -f "${path}/device" 2>/dev/null || true)"
        [[ "$devpath" == *"/usb"* ]] || continue
        [[ "$(cat "${path}/carrier" 2>/dev/null || echo 0)" == "1" ]] || continue
        if ip -4 -o addr show "$iface" 2>/dev/null | grep -q "inet "; then
            echo "$iface"
            return 0
        fi
    done
    return 0
}

# True when apt has an installable candidate for the package.
#
# `apt-get install` is all-or-nothing: a single name it cannot resolve makes it
# reject the whole list. Availability is not uniform across suite and arch —
# Debian ships libfdk-aac-dev in non-free, Raspbian trixie armhf does not — so
# optional packages have to be probed, never assumed.
apt_pkg_available() {
    apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/{print $2}' | grep -qv '^(none)$'
}

# The onboard WiFi interface, if the radio is still live. Onboard chips hang off
# SDIO/mmc, dongles off USB — so the device path, not the name, is what tells
# them apart (a dongle can be called wlan0 too once the onboard radio is gone).
# Echoes nothing when onboard WiFi is already disabled or absent.
onboard_wlan_iface() {
    local wpath iface devpath
    for wpath in /sys/class/net/*/wireless; do
        [[ -e "$wpath" ]] || continue
        iface="$(basename "$(dirname "$wpath")")"
        devpath="$(readlink -f "/sys/class/net/${iface}/device" 2>/dev/null || true)"
        [[ "$devpath" == *"/usb"* ]] && continue
        echo "$iface"
        return 0
    done
    return 0
}

# Interface carrying the current default route — i.e. the link this SSH session
# most likely arrived on. Used as a lockout guard before touching a radio.
default_route_iface() {
    ip -4 route show default 2>/dev/null | awk '/ dev /{for(i=1;i<NF;i++) if($i=="dev"){print $(i+1); exit}}'
}

cfg_get() {
    python3 "$YAML_EDIT" --config "$PLAYER_CONFIG" get "$1"
}

cfg_set() {
    python3 "$YAML_EDIT" --config "$PLAYER_CONFIG" set "$1" "$2"
}

normalize_mac() {
    local mac="$1"
    mac="${mac^^}"
    mac="${mac//-/:}"
    if [[ "$mac" =~ ^([0-9A-F]{2}:){5}[0-9A-F]{2}$ ]]; then
        echo "$mac"
        return 0
    fi
    return 1
}

primary_mac() {
    cfg_get "bluetooth.primary_mac" 2>/dev/null || true
}

bt_ensure_ready() {
    require_cmd bluetoothctl
    bluetoothctl show >/dev/null 2>&1 || die "bluetooth adapter unavailable"
    bluetoothctl power on >/dev/null 2>&1 || true
    # Bondable (mgmt flag, exposed as the adapter's Pairable property) MUST be
    # on when we pair OUT to a speaker, or BlueZ completes the pairing without
    # storing a long-term key: `Paired: yes, Bonded: no`, which evaporates on
    # the next disconnect/restart (no dir under /var/lib/bluetooth). Some boots
    # come up non-bondable, so re-assert it on every BT op.
    bluetoothctl pairable on >/dev/null 2>&1 || true
    bluetoothctl agent NoInputNoOutput >/dev/null 2>&1 || true
    bluetoothctl default-agent >/dev/null 2>&1 || true
}

# One org.bluez.Device1 property, read straight off D-Bus ("b true",
# 's "JBL Go 3"'; empty when the device is unknown). These checks run in
# polling loops, and every bluetoothctl launch registers an agent client and an
# advertisement monitor with bluetoothd: ~1 launch/s with no speaker connected
# (mpv_daemon.sh waiting) is the churn that once stole the pairing agent.
#
# Every adapter, not just hci0: with a USB dongle fitted — the documented
# workaround for the onboard controller — the speaker can live under hci1, and
# a hardcoded hci0 read it as unknown, i.e. never paired and never connected.
# A "true" from any adapter wins (a speaker merely *seen* in a scan on hci0 is
# still paired and connected on hci1); otherwise the first answer. One call on
# a single-adapter box, as before.
bt_dev_prop() {
    local mac="${1^^}" prop="$2" d hci out first=""
    if command -v busctl >/dev/null 2>&1; then
        for d in /sys/class/bluetooth/hci*; do
            hci="${d##*/}"
            [[ "$hci" =~ ^hci[0-9]+$ ]] || continue   # hciN:M = an ACL link
            out="$(busctl get-property org.bluez "/org/bluez/${hci}/dev_${mac//:/_}" \
                org.bluez.Device1 "$prop" 2>/dev/null)" || continue
            [[ "$out" == "b true" ]] && { printf '%s\n' "$out"; return 0; }
            [[ -n "$first" ]] || first="$out"
        done
        [[ -z "$first" ]] || printf '%s\n' "$first"
        return 0
    fi
    # No busctl: same answer, the expensive way.
    bluetoothctl info "$mac" 2>/dev/null | awk -v k="$prop" -F': ' '
        $1 ~ "^[[:space:]]*" k "$" { v = $2; if (v == "yes") print "b true";
            else if (v == "no") print "b false"; else print "s \"" v "\""; exit }'
}

bt_device_connected() { [[ "$(bt_dev_prop "$1" Connected)" == "b true" ]]; }
bt_device_paired() { [[ "$(bt_dev_prop "$1" Paired)" == "b true" ]]; }
# Paired but not bonded = the link key lives only in bluetoothd's memory, so
# restarting bluetooth.service forgets the pairing.
bt_device_bonded() { [[ "$(bt_dev_prop "$1" Bonded)" == "b true" ]]; }

bt_device_name() {
    local v
    v="$(bt_dev_prop "$1" Alias)"
    [[ -n "$v" ]] || v="$(bt_dev_prop "$1" Name)"
    v="${v#s \"}"
    echo "${v%\"}"
}

# Does the device answer a page at all? A speaker that is off, asleep, or held by
# a phone fails here in ~5 s; the connect loop used to spend 3 attempts and 90 s
# finding that out, and the UI got a timeout. Two pings: one missed page
# (radio busy streaming to another device) must not condemn a speaker.
bt_device_reachable() {
    local mac="$1"
    command -v l2ping >/dev/null 2>&1 || return 0  # can't tell: let connect try
    timeout 7 l2ping -c 1 -t 5 "$mac" >/dev/null 2>&1 && return 0
    timeout 7 l2ping -c 1 -t 5 "$mac" >/dev/null 2>&1
}

# Every wireless interface present, whatever it is called. Onboard radios are
# wlan0, but USB dongles often enumerate as wlx<mac> under predictable naming —
# hardcoding wlan0 is why the captive portal never worked on a dongle.
wireless_ifaces() {
    local p iface
    for p in /sys/class/net/*/wireless; do
        [[ -d "$p" ]] || continue
        iface="$(basename "$(dirname "$p")")"
        [[ -n "$iface" ]] && echo "$iface"
    done
}

first_wireless_iface() { wireless_ifaces | head -1; }

# Every IPv4 default gateway, one "<gw> <iface>" per line.
default_gateways() {
    ip -4 route show default 2>/dev/null | awk '
        { gw=""; dev="" }
        { for (i = 1; i < NF; i++) {
              if ($i == "via") gw = $(i+1)
              if ($i == "dev") dev = $(i+1)
          }
          if (gw != "" && dev != "") print gw, dev }'
}

# True when this gateway answers — ICMP first, ARP second.
#
# The ARP fallback is not belt-and-braces: some gateways drop echo requests, and
# a neighbour entry with a hardware address proves frames are crossing the link,
# which is all "is this uplink alive" really asks. The ping is what populates it,
# so the order matters. A wedged adapter fails both: no reply, and the entry goes
# FAILED or INCOMPLETE.
gateway_answers() {
    local gw="$1" dev="$2" neigh
    ping -c1 -W1 -n -q -I "$dev" "$gw" >/dev/null 2>&1 && return 0
    neigh="$(ip -4 neigh show "$gw" dev "$dev" 2>/dev/null)"
    [[ "$neigh" == *lladdr* ]] || return 1
    [[ "$neigh" == *FAILED* || "$neigh" == *INCOMPLETE* ]] && return 1
    return 0
}

# True when this box actually has a way off the network.
#
# This used to mean "some interface has carrier and an IPv4 address", which is a
# statement about configuration, not connectivity. During an eight-minute outage
# the USB dongle kept carrier, its address and the default route while passing no
# traffic at all (a port wedged with error -71 does exactly this), so the captive
# watcher saw a healthy uplink the whole time and never raised the setup AP.
#
# So: ask the default gateway. The gateway and not the internet — a WAN outage is
# not this box's problem to solve, and raising an AP over it would take the UI off
# the LAN for no reason. No default route at all is likewise no uplink; that is
# the state a freshly plugged, never-configured adapter sits in.
#
# UPLINK_PROBE=0 restores the old configuration-only test, for a link where
# neither ICMP nor ARP can work (a point-to-point modem, say).
have_uplink() {
    local gw dev
    if [[ "${UPLINK_PROBE:-1}" == "0" ]]; then
        [[ -n "$(default_route_iface)" ]] && return 0
        return 1
    fi
    while read -r gw dev; do
        [[ -n "$gw" && -n "$dev" ]] || continue
        gateway_answers "$gw" "$dev" && return 0
    done < <(default_gateways)
    return 1
}

# True while a user-initiated pair/connect/scan is in flight. Anything that
# would page, suspend or tear down the radio must stand down for it — a wedge
# watchdog unbinding the driver mid-pair is indistinguishable, to the user, from
# the pairing simply failing. Moved here from bt_autoconnect.sh so the watchdog
# reuses the tested implementation rather than growing a second one.
bt_ops_busy() {
    local pairing="${RUN_DIR}/pairing.active" lock="${RUN_DIR}/btctl.lock"
    local scan="${RUN_DIR}/scan.active"
    # The scan sentinel holds a deadline epoch and expires by itself, so a
    # killed scan cannot wedge this permanently.
    if [[ -e "$scan" ]]; then
        local deadline now
        deadline="$(cat "$scan" 2>/dev/null || echo 0)"
        now="$(date +%s)"
        if [[ "$deadline" =~ ^[0-9]+$ ]] && (( now < deadline )); then
            return 0
        fi
    fi
    if [[ -e "$pairing" ]]; then
        local age
        age=$(( $(date +%s) - $(stat -c %Y "$pairing" 2>/dev/null || echo 0) ))
        (( age < 120 )) && return 0
    fi
    [[ -e "$lock" ]] || return 1
    exec 8>"$lock"
    if ! flock -n 8; then
        exec 8>&-
        return 0
    fi
    flock -u 8
    exec 8>&-
    return 1
}

# The hciN that belongs to the on-board serdev UART controller, if any. Never
# assume hci0: add a USB Bluetooth dongle — the documented workaround for boards
# whose onboard controller misbehaves — and the numbering is whatever order the
# two enumerated in. Recovering the wrong adapter is worse than recovering none.
bt_serdev_hci() {
    local d dev
    for d in /sys/class/bluetooth/hci*; do
        # /sys/class/bluetooth also holds ACL *connection* nodes named hciN:M,
        # whose device path runs through the adapter and therefore matches the
        # same serial pattern. Adapters only.
        [[ "$(basename "$d")" =~ ^hci[0-9]+$ ]] || continue
        [[ -e "${d}/device" ]] || continue
        dev="$(readlink -f "${d}/device" 2>/dev/null || true)"
        [[ "$dev" == *"/serial"*"-"* ]] || continue
        basename "$d"
        return 0
    done
    return 1
}

# --- Bluetooth adapter state ------------------------------------------------
#
# One reader for "is this controller up", because the two callers have opposite
# polarity — bt_recover.sh retries when it is not up, bt_watchdog.sh escalates —
# and a second copy is exactly how they came to disagree about the cases where
# the answer is not knowable.
#
# Prints one of:
#   up       the adapter exists and is UP
#   down     the adapter exists and is not UP
#   absent   there is no such adapter
#   unknown  hciconfig could not be asked. NOT the same as down: hciconfig is
#            deprecated and may simply be gone. Anything destructive must treat
#            this as "do nothing", or a missing tool reads as a wedged radio.
bt_hci_state() {
    local hci="${1:-}" out
    [[ -n "$hci" && -e "/sys/class/bluetooth/${hci}" ]] || { echo absent; return 0; }
    command -v hciconfig >/dev/null 2>&1 || { echo unknown; return 0; }
    out="$(hciconfig "$hci" 2>/dev/null || true)"
    [[ -n "$out" ]] || { echo unknown; return 0; }
    if grep -qE '^[[:space:]]*UP' <<<"$out"; then echo up; else echo down; fi
}

# No `exit` in the awk: quitting early kills the producer with SIGPIPE, and
# under `set -o pipefail` that fails the assignment this usually feeds — which
# errexit then turns into a silent abort of the caller. The input is a handful
# of lines; read all of it and keep the first match.
bt_hci_bdaddr() {
    hciconfig "${1:?}" 2>/dev/null \
        | awk '/BD Address/ && !seen {print $3; seen = 1}' || true
}

# All-zero BD address means the firmware patch never loaded — the chip is there
# as far as the driver is concerned, and dead as far as anything else is.
bt_hci_initialised() {
    local a
    a="$(bt_hci_bdaddr "${1:?}" || true)"
    [[ -n "$a" && "$a" != "00:00:00:00:00:00" ]]
}

# Whether unbinding the driver can actually power-cycle the BT chip.
#
# Unbinding releases the BT_ON line and drives it low. That resets the chip only
# where BT_ON is a real SoC GPIO (Zero 2W: pinctrl-bcm2835). Where it sits on the
# VideoCore-managed expander (Pi 3 A+/4: raspberrypi-exp-gpio) the line moves and
# the chip does not, so the rebind fails at the vendor baudrate command and
# leaves the adapter dead — from a *healthy* starting state just as readily as
# from a wedged one. Verified on both boards 2026-08-08.
#
# Prints yes | no | unknown, and `unknown` is the important one: every caller is
# destructive, so "cannot tell" must never be spelled "yes". Probes, in order of
# how much they actually prove:
#   1. the label of the GPIO chip that carries BT_ON — needs libgpiod's
#      gpioinfo, which install.sh installs (gpiod) for exactly this
#   2. the expander existing at all: it is only present on the boards that put
#      BT_ON behind it, so finding one is a sound "no"
#   3. the board model, for the two boards this project is verified on
bt_rebind_verdict() {
    local chip label d
    # Same SIGPIPE trap as bt_hci_bdaddr, and this one bit on real hardware:
    # awk exiting at the match killed gpioinfo mid-write, pipefail returned 141,
    # and errexit abandoned the function — so a Zero 2W, where the rebind is the
    # recovery that works, answered nothing at all and was treated as "unknown".
    chip="$(gpioinfo 2>/dev/null | awk '/^gpiochip/{c=$1} /BT_ON/ && !seen {print c; seen = 1}' || true)"
    if [[ -n "$chip" ]]; then
        # Only /sys/bus/gpio/devices is keyed by chip *name*. The legacy
        # /sys/class/gpio tree names its directories after the GPIO base (512+
        # under dynamic allocation), so "gpiochip0" never appears there and
        # looking for it was a no-op.
        d="/sys/bus/gpio/devices/${chip}/label"
        if [[ -e "$d" ]]; then
            label="$(cat "$d" 2>/dev/null || true)"
            [[ "$label" == *exp-gpio* ]] && { echo no; return 0; }
            [[ -n "$label" ]] && { echo yes; return 0; }
        fi
    fi
    for d in /sys/bus/gpio/devices/*/label /sys/class/gpio/gpiochip*/label; do
        [[ -e "$d" ]] || continue
        grep -q "exp-gpio" "$d" 2>/dev/null && { echo no; return 0; }
    done
    case "$(platform_board 2>/dev/null || echo other)" in
        zero2w)   echo yes ;;   # BT_ON on pinctrl-bcm2835, rebind verified
        pi3aplus) echo no ;;    # BT_ON on the expander, rebind verified fatal
        *)        echo unknown ;;
    esac
}

# True only when a rebind is *known* to work. "Cannot tell" is a refusal, not a
# green light — the rebind kills a healthy controller wherever it does not apply.
bt_rebind_viable() { [[ "$(bt_rebind_verdict)" == "yes" ]]; }

# Printable form, for install.sh — it shells out through common_query and only
# ever sees stdout, never an exit status.
bt_rebind_viable_str() { bt_rebind_verdict; }

MPV_UNIT="${MPV_UNIT:-audio-player-mpv.service}"

mpv_socket_ready() { [[ -S "${RUN_DIR}/mpv.sock" ]]; }

# mpv_daemon.sh exits when no BT sink is up at startup, and systemd only retries
# on a ~2 min cycle — so the IPC socket is routinely missing long after a device
# actually connected, and every playback call 503s meanwhile. Note `try-restart`
# is a no-op on a failed/inactive unit, i.e. in exactly the case that needs the
# repair, so these always use plain `restart`.
# --no-block is required, not cosmetic: mpv.service is ordered
# After=audio-player-bt-autoconnect.service, so a blocking restart issued from
# inside that unit would wait on a job systemd cannot run until it exits.
mpv_restart_now() {
    command -v systemctl >/dev/null 2>&1 || return 0
    log "restarting ${MPV_UNIT}${1:+ (${1})}"
    systemctl restart --no-block "$MPV_UNIT" >/dev/null 2>&1 || true
}

# Start mpv only when it is actually down — a live socket is left alone so this
# never interrupts playback that is already running.
mpv_ensure_running() {
    mpv_socket_ready && return 0
    mpv_restart_now "${1:-IPC socket missing}"
}

# ALSA device string for mpv/aplay: bluealsa:DEV=MAC,PROFILE=a2dp
bt_mpv_audio_device() {
    local mac="$1"
    echo "alsa/bluealsa:DEV=${mac},PROFILE=a2dp"
}

bt_bluealsa_pcm() {
    local mac="$1"
    echo "bluealsa:DEV=${mac},PROFILE=a2dp"
}

write_bt_status() {
    local json="$1"
    mkdir -p "$STATE_DIR"
    # tmp + rename: a power cut mid-write must leave the old status, not an
    # empty file the UI then reads as garbage.
    printf '%s\n' "$json" > "${BT_STATUS_FILE}.tmp" && mv -f "${BT_STATUS_FILE}.tmp" "$BT_STATUS_FILE"
}

# Devices that stall on a codec are recorded here by bt_codec_select.sh
# (one "MAC CODEC" per line) and skipped by auto-select / best_codec.
BT_CODEC_DENYLIST="${BT_CODEC_DENYLIST:-${STATE_DIR}/codec_denylist}"

# Rank A2DP codecs for "best quality" decisions (higher is better)
codec_rank() {
    case "$1" in
        LDAC) echo 5 ;;
        aptX-HD) echo 4 ;;
        aptX) echo 3 ;;
        AAC) echo 2 ;;
        SBC) echo 1 ;;
        *) echo 0 ;;
    esac
}

# True if this device is known to stall on the codec
bt_codec_denied() {
    local mac="$1" codec="$2"
    [[ -f "$BT_CODEC_DENYLIST" ]] && grep -qixF "${mac} ${codec}" "$BT_CODEC_DENYLIST"
}

# Print the bluealsa a2dp-source PCM D-Bus path for a device (empty if none).
# Never returns non-zero, so `var="$(bt_a2dp_pcm_path ...)"` is safe under
# set -e even when no PCM exists yet.
bt_a2dp_pcm_path() {
    local mac="$1"
    command -v bluealsa-cli >/dev/null 2>&1 || return 0
    bluealsa-cli list-pcms 2>/dev/null \
        | grep -i "dev_${mac//:/_}" | grep -F "/a2dpsrc/" | head -1 | awk '{print $1}' || true
}

# True only when bluealsa can be asked AND has no a2dp-source PCM for MAC.
# Without bluealsa-cli (stock Bookworm bluez-alsa) the answer is unknowable, and
# unknowable must not read as missing: it made every 45 s retry tick reconnect a
# speaker that was streaming fine, and codec-prime it — a test clip each time.
bt_a2dp_missing() {
    command -v bluealsa-cli >/dev/null 2>&1 || return 1
    [[ -z "$(bt_a2dp_pcm_path "$1")" ]]
}

# Wait until the a2dp-source playback PCM exists in bluealsa (arg 2 = seconds).
# Returns 0 immediately on stock installs without bluealsa-cli.
bt_wait_a2dp_pcm() {
    local mac="$1" secs="${2:-12}" i=0 max
    command -v bluealsa-cli >/dev/null 2>&1 || return 0
    max=$((secs * 2))
    while (( i < max )); do
        [[ -n "$(bt_a2dp_pcm_path "$mac")" ]] && return 0
        sleep 0.5
        i=$((i + 1))
    done
    return 1
}

# Wait until BlueZ knows the device (pre-pair discovery). Returns fast when
# already cached; otherwise scans and stops on first sighting instead of
# burning the whole discovery window.
bt_wait_device_visible() {
    local mac="$1" secs="${2:-15}" scanner="" deadline
    if bluetoothctl devices 2>/dev/null | grep -qi "^Device ${mac}"; then
        return 0
    fi
    bluetoothctl --timeout "$secs" scan on >/dev/null 2>&1 &
    scanner=$!
    deadline=$((SECONDS + secs))
    while (( SECONDS < deadline )); do
        if bluetoothctl devices 2>/dev/null | grep -qi "^Device ${mac}"; then
            kill "$scanner" 2>/dev/null || true
            wait "$scanner" 2>/dev/null || true
            return 0
        fi
        sleep 0.5
    done
    wait "$scanner" 2>/dev/null || true
    return 1
}

# BlueZ caches remote A2DP endpoints per device; after the local media
# application changes (e.g. new codecs), the stale cache makes every
# profile connect fail with "Unable to select SEP". Wiping the cache file
# (NOT the pairing keys in info) forces a fresh AVDTP discovery.
bt_clear_endpoint_cache() {
    local mac="$1"
    # The restart below drops every in-memory pairing: an unbonded speaker
    # (IKEA KALLSUP) would need pairing again, which is worse than the cache.
    if bt_device_paired "$mac" && ! bt_device_bonded "$mac"; then
        warn "not clearing BlueZ endpoint cache for ${mac}: it is paired but not bonded, and restarting bluetoothd would forget the pairing"
        return 1
    fi
    warn "clearing BlueZ endpoint cache for ${mac} (stale SEPs) — restarting bluetoothd"
    systemctl stop bluetooth
    rm -f /var/lib/bluetooth/*/cache/"$mac" 2>/dev/null || true
    systemctl start bluetooth
    sleep 3
    systemctl try-restart bluealsa.service 2>/dev/null || true
    sleep 2
    bt_ensure_ready
}

# SBC-XQ (dual channel) vs standard SBC is chosen per speaker, but it can only be
# chosen at negotiation: a live AVDTP reconfigure made both the IKEA KALLSUP and
# the JBL Go 3 drop the link (Close timed out, then "Host is down"). bluealsa's
# --sbc-quality decides what it negotiates, so set that before connecting.
# /etc runs xq (the default); a runtime drop-in in /run switches to high for a
# speaker denylisted as "SBC-XQ". tmpfs: no SD write, gone on reboot.
BLUEALSA_ETC_DROPIN=/etc/systemd/system/bluealsa.service.d/10-hd-codecs.conf
BLUEALSA_RUN_DROPIN=/run/systemd/system/bluealsa.service.d/50-sbc-quality.conf

bt_sbc_quality_for() {
    if bt_codec_denied "$1" "SBC-XQ"; then echo high; else echo xq; fi
}

bt_bluealsa_quality() {
    systemctl show bluealsa.service -p ExecStart --value 2>/dev/null \
        | grep -oE -- '--sbc-quality=[a-z]+' | head -1 | cut -d= -f2 || true
}

# Restart bluealsa in the mode MAC needs, if it isn't already. Only for an
# about-to-connect device: a restart drops whatever is streaming.
bt_prepare_bluealsa() {
    local mac="$1" want cur base
    [[ -f "$BLUEALSA_ETC_DROPIN" ]] || return 0  # stock bluealsa: no modes to pick
    want="$(bt_sbc_quality_for "$mac")"
    cur="$(bt_bluealsa_quality)"
    [[ -n "$cur" && "$cur" != "$want" ]] || return 0
    log "bluealsa: SBC quality ${cur} -> ${want} for ${mac}"
    if [[ "$want" == "xq" ]]; then
        rm -f "$BLUEALSA_RUN_DROPIN"
    else
        base="$(grep -E '^ExecStart=.+' "$BLUEALSA_ETC_DROPIN" | tail -1 | cut -d= -f2-)"
        [[ -n "$base" ]] || return 0
        mkdir -p "$(dirname "$BLUEALSA_RUN_DROPIN")"
        printf '# Written by bt_prepare_bluealsa (_common.sh) for %s\n[Service]\nExecStart=\nExecStart=%s\n' \
            "$mac" "$(sed -E "s/--sbc-quality=[a-z]+/--sbc-quality=${want}/" <<<"$base")" \
            > "$BLUEALSA_RUN_DROPIN"
    fi
    systemctl daemon-reload
    systemctl restart bluealsa.service || { warn "bluealsa restart failed"; return 0; }
    local i
    for i in {1..20}; do
        bluealsa-cli status >/dev/null 2>&1 && return 0
        sleep 0.5
    done
    warn "bluealsa not answering after restart"
}

bt_connect_session() {
    local mac="$1"
    local logfile="/tmp/bt_connect_${mac//:/_}.log"
    local lock="${RUN_DIR}/btctl.lock"
    mkdir -p "$RUN_DIR"
    exec 9>"$lock"
    flock -w 30 9 || die "bluetoothctl busy (another connect in progress)"
    bt_ensure_ready
    : >"$logfile"
    bluetoothctl trust "$mac" >>"$logfile" 2>&1 || true
    # Connected is not enough. After bluetoothd or bluealsa restarts a speaker
    # can sit "Connected" with no A2DP stream for minutes (ACL + AVRCP only),
    # and returning here left it silent. `bluetoothctl connect` on a connected
    # device brings up the missing profiles — and, Pi-initiated, it also gets
    # AVRCP volume registered, which a speaker-initiated reconnect skipped.
    if bt_device_connected "$mac"; then
        bt_a2dp_missing "$mac" || return 0
        log "connected without an A2DP stream — connecting the audio profile"
    fi
    bt_prepare_bluealsa "$mac"
    # One-shot connect blocks on the D-Bus Connect() reply — event-driven,
    # unlike the old fixed-sleep heredoc. Profiles can trail the reply, so
    # verify with a short poll.
    timeout 20 bluetoothctl connect "$mac" >>"$logfile" 2>&1 || true
    local i
    for i in {1..10}; do
        bt_device_connected "$mac" && return 0
        sleep 0.5
    done
    return 1
}

# Known-devices memory for the UI "My devices" list and autoconnect ordering.
KNOWN_DEVICES_FILE="${KNOWN_DEVICES_FILE:-${STATE_DIR}/known_devices.json}"

# Record a successful connect (name, timestamp, count). Deduped to one write
# per 60 s per device (SD wear); atomic tmp+mv. Never fails the caller.
bt_mark_connected() {
    local mac="$1" name="${2:-}"
    command -v jq >/dev/null 2>&1 || return 0
    mkdir -p "$STATE_DIR"
    local now epoch prev cur
    now="$(date -Iseconds)"
    epoch="$(date +%s)"
    cur="$(cat "$KNOWN_DEVICES_FILE" 2>/dev/null || echo '{}')"
    echo "$cur" | jq -e . >/dev/null 2>&1 || cur='{}'
    prev="$(echo "$cur" | jq -r --arg mac "$mac" '.devices[$mac].last_connected_epoch // 0')"
    (( epoch - prev < 60 )) && return 0
    echo "$cur" | jq --arg mac "$mac" --arg name "$name" --arg now "$now" --argjson epoch "$epoch" '
        .version = 1
        | .devices = (.devices // {})
        | .devices[$mac] = ((.devices[$mac] // {}) + {
            name: (if $name != "" then $name else (.devices[$mac].name // null) end),
            last_connected: $now,
            last_connected_epoch: $epoch,
            connect_count: ((.devices[$mac].connect_count // 0) + 1)
        })' > "${KNOWN_DEVICES_FILE}.tmp" \
        && mv "${KNOWN_DEVICES_FILE}.tmp" "$KNOWN_DEVICES_FILE" \
        || warn "known_devices update failed for ${mac}"
}

bt_known_forget() {
    local mac="$1"
    command -v jq >/dev/null 2>&1 || return 0
    [[ -f "$KNOWN_DEVICES_FILE" ]] || return 0
    jq --arg mac "$mac" 'del(.devices[$mac])' "$KNOWN_DEVICES_FILE" > "${KNOWN_DEVICES_FILE}.tmp" \
        && mv "${KNOWN_DEVICES_FILE}.tmp" "$KNOWN_DEVICES_FILE" \
        || warn "known_devices forget failed for ${mac}"
}
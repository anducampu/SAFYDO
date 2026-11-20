#!/usr/bin/env bash
# SAFYDO — board + OS detection.
#
# Everything platform-specific in this repo keys off two values:
#   board  zero2w | pi3aplus | other      (/proc/device-tree/model)
#   os     bookworm | trixie | other      (/etc/os-release VERSION_CODENAME)
#
# Sourced by scripts/_common.sh (so every script inherits it) and by install.sh.
# Safe to source off-Pi: everything degrades to "other" instead of failing.
#
# The two axes are INDEPENDENT — every predicate below keys off exactly one of
# them, so all four combinations are supported. Board decides hardware traits,
# OS decides package traits:
#   board zero2w    — 2.4 GHz-only radio, USB OTG gadget dev link
#   board pi3aplus  — dual-band radio, single USB-A host port, no gadget
#   os    bookworm  — bluez 5.66, bluez-alsa 4.0.0 (no bluealsa-cli)
#   os    trixie    — bluez 5.82, bluez-alsa 4.3.1, /tmp already tmpfs
# Never gate a decision on a board+OS pair; pick the axis that actually causes
# the difference. Anything unrecognised runs the generic path.

PLATFORM_CACHE="${PLATFORM_CACHE:-/run/audio-player/platform}"

# --- raw detection ------------------------------------------------------------

_platform_model_string() {
    [[ -r /proc/device-tree/model ]] || return 0
    tr -d '\0' < /proc/device-tree/model 2>/dev/null || true
}

# zero2w | pi3aplus | other
platform_board() {
    if [[ -n "${SAFYDO_BOARD:-}" ]]; then
        echo "$SAFYDO_BOARD"
        return 0
    fi
    local model
    model="$(_platform_model_string)"
    case "$model" in
        *"Zero 2"*)          echo zero2w ;;
        *"3 Model A Plus"*)  echo pi3aplus ;;
        *)                   echo other ;;
    esac
}

# bookworm | trixie | other
platform_os() {
    if [[ -n "${SAFYDO_OS:-}" ]]; then
        echo "$SAFYDO_OS"
        return 0
    fi
    local codename=""
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        codename="$(. /etc/os-release 2>/dev/null && echo "${VERSION_CODENAME:-}")"
    fi
    case "$codename" in
        bookworm|trixie) echo "$codename" ;;
        *)               echo other ;;
    esac
}

# Human-readable one-liner for logs: "Raspberry Pi 3 Model A Plus Rev 1.0 [pi3aplus/trixie]"
platform_describe() {
    local model
    model="$(_platform_model_string)"
    echo "${model:-unknown hardware} [$(platform_board)/$(platform_os)]"
}

# --- capability predicates ----------------------------------------------------

# USB gadget dev link (serial ttyGS0 + usb0 ethernet — docs/usb-dev-link.md).
# Zero 2W only: its micro-USB data port is a real OTG port and dwc2 is already
# the host driver there. On the 3 A+ the OTG ID pin is tied to 0 V (host-only
# unless dr_mode=peripheral + a USB-A-to-A cable), and loading dwc2 in place of
# dwc_otg costs USB host throughput — which is the path the network dongle
# carries the audio stream over. FORCE_USB_GADGET=1 overrides for experiments.
board_supports_gadget() {
    [[ "${FORCE_USB_GADGET:-}" == "1" ]] && return 0
    [[ "$(platform_board)" == "zero2w" ]]
}

# config.txt conditional filter for this board, so appended fragments never
# apply to hardware they were not written for. Empty (== [all]) when unknown.
board_config_filter() {
    case "$(platform_board)" in
        zero2w)   echo "[pi02]" ;;
        pi3aplus) echo "[pi3]" ;;
        *)        echo "" ;;
    esac
}

# Board-specific overclock fragment (opt-in via APPLY_OVERCLOCK=1).
board_overclock_fragment() {
    case "$(platform_board)" in
        zero2w)   echo "config-overclock.fragment" ;;
        pi3aplus) echo "config-overclock-pi3aplus.fragment" ;;
        *)        echo "" ;;
    esac
}

# Onboard WiFi can use 5 GHz → WiFi and BT need not fight over 2.4 GHz.
board_wifi_dualband() {
    [[ "$(platform_board)" == "pi3aplus" ]]
}

# --- cache --------------------------------------------------------------------

# Write the detected platform to /run for the backend + diagnostics. Best effort.
platform_write_cache() {
    local dir
    dir="$(dirname "$PLATFORM_CACHE")"
    mkdir -p "$dir" 2>/dev/null || return 0
    {
        echo "board=$(platform_board)"
        echo "os=$(platform_os)"
        echo "model=$(_platform_model_string)"
    } > "$PLATFORM_CACHE" 2>/dev/null || true
}

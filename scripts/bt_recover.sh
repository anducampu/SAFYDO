#!/usr/bin/env bash
# Recover a Bluetooth controller that has stopped answering the host.
#
# The failure this exists for: mid-stream, the controller stops carrying ACL
# data and stops answering HCI commands at the same instant. The kernel logs
# `command tx timeout` every couple of seconds; `hciconfig` still reports
# UP RUNNING, BlueZ still reports the device connected, bluealsa still reports
# the transport Running. Nothing above the controller notices, because nothing
# above the controller is told. Seen on both supported boards, on different
# chips, kernels, BlueZ versions and OS releases.
#
# What works: power the chip down and let the driver load its firmware again.
# Unbinding hci_uart_bcm releases the BT_ON line and drives it low; five seconds
# later a rebind re-runs the whole init — firmware patch, BD address, the lot.
#
#   Verified on the Zero 2W (2026-08-08): a controller wedged for four minutes,
#   102 command timeouts, recovered in ~16 s with the box still up. `hciconfig
#   hci0 reset` and `bluetoothctl power off/on` both fail on a wedged chip and
#   leave it DOWN — this is the only thing that brought it back short of a boot.
#
# What does NOT work, and why it is board-specific: on the Pi 3 A+ the BT_ON
# line lives on the VideoCore-managed expander rather than the SoC's own GPIO
# controller, and driving it there does not truly cut the chip's enable. The
# rebind then fails at the vendor baudrate command with `-110` and a BD address
# of all zeros — and it fails that way even starting from a *healthy* chip, so a
# failed rebind on that board says nothing about the controller's state. There,
# a reboot remains the only recovery.
#
# Usage:
#   sudo bt_recover.sh              # recover now
#   sudo bt_recover.sh --dry-run    # report what it would do, touch nothing
#   sudo bt_recover.sh --anyway     # rebind even where it is known not to work
#   OFF_SECONDS=8 sudo bt_recover.sh
#
# Reconnecting the speaker afterwards is left to audio-player-bt-retry.timer,
# which already does exactly that within 45 s.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

DRIVER_DIR="${DRIVER_DIR:-/sys/bus/serial/drivers/hci_uart_bcm}"
OFF_SECONDS="${OFF_SECONDS:-5}"
usage() {
    cat >&2 <<'EOF'
Usage: bt_recover.sh [--dry-run] [--anyway]
  --dry-run   report the controller, the board verdict and the plan; touch nothing
  --anyway    rebind even where the verdict says it cannot work (testing only)
  OFF_SECONDS=8  how long to hold BT_ON low (default 5)
EOF
}

DRY_RUN=false
ANYWAY=false
# No default branch here once meant that a typo — --dryrun, -n, --help — fell
# straight through to a real unbind. Anything unrecognised stops the script.
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --anyway)  ANYWAY=true ;;   # override the viability refusal, for testing
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: ${arg} (--dry-run|--anyway|--help)" ;;
    esac
done

# Never falls back to hci0. Empty is the honest answer when the serdev adapter
# is gone, and the wrong one is worse than none: with a USB dongle in the box —
# the documented workaround for this very fault — hci0 is the dongle, and the
# post-rebind check would have reported its health as this chip's recovery.
# Re-resolved after the rebind too, because the adapter can come back numbered
# differently than it went away.
HCI=""
refresh_hci() { HCI="$(bt_serdev_hci 2>/dev/null || true)"; }
refresh_hci

# State reading lives in _common.sh, shared with bt_watchdog.sh: the two have
# opposite polarity and drifted apart over exactly the ambiguous cases.
hci_present()     { [[ -n "$HCI" && "$(bt_hci_state "$HCI")" != "absent" ]]; }
hci_up()          { [[ -n "$HCI" ]] && [[ "$(bt_hci_state "$HCI")" == "up" ]]; }
hci_bdaddr()      { [[ -n "$HCI" ]] && bt_hci_bdaddr "$HCI"; }
hci_initialised() { [[ -n "$HCI" ]] && bt_hci_initialised "$HCI"; }
# Display only, never a decision: the flags line is what a human reading the log
# wants to see ("UP RUNNING PSCAN"), which the one-word state does not carry.
hci_flags()       { [[ -n "$HCI" ]] && hciconfig "$HCI" 2>/dev/null | sed -n 3p | tr -d '\t'; }

serdev_device() {
    local d
    for d in /sys/bus/serial/devices/*; do
        [[ -e "${d}/driver" ]] || continue
        [[ "$(readlink -f "${d}/driver")" == "$DRIVER_DIR" ]] || continue
        basename "$d"
        return 0
    done
    # Unbound already (a previous run died half way, or someone unbound it by
    # hand): fall back to the conventional name so we can still rebind.
    [[ -e /sys/bus/serial/devices/serial0-0 ]] && { echo "serial0-0"; return 0; }
    return 1
}

main() {
    [[ "${EUID}" -eq 0 ]] || die "run as root: sudo $0"
    [[ -d "$DRIVER_DIR" ]] || die "hci_uart_bcm not present — this board's BT is not on a serdev UART"

    local dev
    dev="$(serdev_device)" || die "no serial device bound to hci_uart_bcm and no serial0-0 to rebind"

    local verdict
    verdict="$(bt_rebind_verdict)"

    # The report comes first, and unconditionally: --dry-run is the diagnostic
    # this script offers, and it is most worth having on the board where the
    # rebind is refused. Refusing before reporting made it print four warnings
    # and exit 2 without ever naming the controller.
    log "controller: ${HCI:-none} $(hci_present && echo "$(bt_hci_state "$HCI") $(hci_flags)" || echo missing) bdaddr=$(hci_bdaddr || echo none)"
    log "board: $(platform_board 2>/dev/null || echo unknown), rebind recovers this board: ${verdict}"
    log "plan: unbind ${dev}, hold ${OFF_SECONDS}s with BT_ON low, rebind"

    # Refuse where this is destructive rather than "try and see": the rebind
    # fails from a healthy controller just as readily as from a wedged one, so
    # attempting it can only ever make things worse. `unknown` refuses too —
    # a guess in this direction costs a working radio.
    if [[ "$verdict" != "yes" ]] && ! $ANYWAY; then
        if $DRY_RUN; then
            log "[dry-run] would NOT rebind (verdict: ${verdict}); a reboot is the recovery here"
            return 0
        fi
        case "$verdict" in
            no) warn "not rebinding: BT_ON is on the VideoCore expander on this board, where"
                warn "unbinding releases the line without power-cycling the chip — it kills a"
                warn "healthy controller as readily as a wedged one." ;;
            *)  warn "not rebinding: cannot establish whether unbinding power-cycles the chip"
                warn "on this board (install gpiod for the precise probe). Refusing to guess:"
                warn "guessing wrong costs a working controller." ;;
        esac
        warn "Reboot is the recovery here:  sudo systemctl reboot"
        warn "(--anyway overrides, for testing.)"
        return 2
    fi

    if $DRY_RUN; then
        log "[dry-run] would rebind (verdict: ${verdict}); nothing touched"
        return 0
    fi

    log "unbinding ${dev}"
    echo "$dev" > "${DRIVER_DIR}/unbind" 2>/dev/null || warn "unbind failed (already unbound?)"
    sleep 1

    # Best-effort observation, never a gate: the line is only readable while no
    # driver holds it, and the tool name/syntax differs across libgpiod 1 and 2.
    local val=""
    if command -v gpiofind >/dev/null 2>&1 && command -v gpioget >/dev/null 2>&1; then
        # shellcheck disable=SC2046
        val="$(gpioget $(gpiofind BT_ON) 2>/dev/null || true)"
    fi
    [[ -n "$val" ]] && log "BT_ON reads ${val} while unbound (0 = chip powered down)"

    log "holding ${OFF_SECONDS}s"
    sleep "$OFF_SECONDS"

    log "rebinding ${dev}"
    echo "$dev" > "${DRIVER_DIR}/bind" || die "rebind failed — a reboot is the remaining option"

    local i
    for ((i = 0; i < 20; i++)); do
        refresh_hci
        hci_present && hci_initialised && hci_up && break
        sleep 1
    done

    if hci_present && hci_initialised && hci_up; then
        log "recovered: ${HCI} $(bt_hci_state "$HCI") $(hci_flags) bdaddr=$(hci_bdaddr)"
        log "the retry timer will reconnect the speaker within 45s"
        return 0
    fi

    warn "controller did not come back: ${HCI:-no serdev adapter}=$(hci_present && echo present || echo missing) bdaddr=$(hci_bdaddr || echo none)"
    warn "Reboot to recover:  sudo systemctl reboot"
    return 1
}

main "$@"

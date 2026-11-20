#!/usr/bin/env bash
# Notice a Bluetooth controller that has stopped answering, and put it back.
#
# The fault (both boards, 2026-08-08): mid-stream the controller stops carrying
# ACL data and stops answering HCI commands in the same instant. The kernel says
# so; nothing above it does — `hciconfig` still reports UP RUNNING, BlueZ still
# reports the device connected, bluealsa still reports the transport Running. So
# without this the box plays silence, retrying into a dead radio, until someone
# reboots it.
#
# Detection is the kernel's own words, not the silence, and only the words this
# adapter said: every line must carry the `Bluetooth: <hci>:` prefix of the
# controller being policed, within the current boot. Unanchored matching counted
# any subsystem's "hardware error 0x00", and counted a USB dongle's faults
# against the onboard chip.
#
# The three recorded signatures do not carry equal weight, because they do not
# appear in equal numbers. One self-reported hardware error is a wedge on its
# own; `command tx timeout` repeats every two seconds, so a single one is noise.
# Scores are summed over the window and compared to EVENT_THRESHOLD (3):
#   hardware error <n>          3   controller self-report, seen once per wedge
#   Opcode 0x0c03 failed        2   HCI_Reset timing out; bt-retry issues ~2/window
#   command tx timeout          1   repeats for as long as the wedge lasts
# The adapter reading DOWN counts too, but only when confirmed twice; the
# adapter having vanished while its driver is still bound counts immediately.
#
# Recovery ladder, and it is board-shaped:
#   1. bt_recover.sh — unbind, five seconds with BT_ON low, rebind. Only where
#      bt_rebind_verdict() says BT_ON is a real SoC GPIO. Where it says "no" or
#      "cannot tell", this step *destroys a healthy controller*, so it is
#      skipped rather than attempted and hoped for.
#   2. Reboot — off unless `bluetooth.wedge_reboot` is true (Settings →
#      Bluetooth in the web UI). On expander boards it is the only recovery
#      there is, and the UI says so, but rebooting the machine is the user's
#      call to make, not the installer's.
#
# Guards, because a watchdog that thrashes is worse than none:
#   - stands down for user pair/connect/scan (bt_ops_busy), re-checked
#     immediately before acting rather than only at the top of the run
#   - stands down while bluetoothd is still settling after a restart
#   - a DOWN adapter must read DOWN twice, seconds apart; a state that cannot be
#     read at all (no hciconfig) is never "wedged"
#   - never reboots the box while a second, healthy adapter is present: on a box
#     using the documented USB-dongle workaround, audio is on the dongle
#   - cooldown between attempts, monotonic within a boot so a late NTP step
#     cannot freeze it, kept in STATE_DIR so a reboot does not erase it
#   - rolling 24 h reboot cap that fails CLOSED: an unreadable ledger, or a
#     clock that has not been trusted by NTP yet, refuses escalation rather
#     than handing back a fresh budget
#   - never exits non-zero for an expected outcome, so `systemctl --failed`
#     stays meaningful; the verdict goes to a status file, which the backend
#     reports at /api/bt/status and diag.sh prints
#
# Usage:
#   bt_watchdog.sh                    # one check (the timer calls this)
#   bt_watchdog.sh --dry-run          # report the verdict, change nothing
#   bt_watchdog.sh --force            # recover now; will NOT reboot
#   bt_watchdog.sh --force --confirm-reboot   # ...and may reboot
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

# Wider than the timer interval plus its accuracy plus a slow run, so evidence
# cannot fall between two windows: 60 + 10 + a ~6 s run, with headroom.
WINDOW_SEC="${WINDOW_SEC:-100}"
EVENT_THRESHOLD="${EVENT_THRESHOLD:-3}"
COOLDOWN_SEC="${COOLDOWN_SEC:-300}"
REBOOT_MAX_PER_DAY="${REBOOT_MAX_PER_DAY:-3}"
CONFIRM_DOWN_SEC="${CONFIRM_DOWN_SEC:-5}"
# bluetoothd restarts (bt_clear_endpoint_cache, install.sh, an apt upgrade) leave
# the adapter down for a moment while it re-powers. That is not a wedge.
SETTLE_SEC="${SETTLE_SEC:-30}"

# Same name and default as bt_recover.sh, and overridable for the same reason:
# it is the one path that decides whether this board is in scope at all.
DRIVER_DIR="${DRIVER_DIR:-/sys/bus/serial/drivers/hci_uart_bcm}"

STATE_FILE="${STATE_DIR}/bt_wedge_recovery.json"
STATUS_FILE="${RUN_DIR}/bt_watchdog.status"
STATUS_PERSIST="${STATE_DIR}/bt_watchdog.status"

DRY_RUN=false
FORCE=false
CONFIRM_REBOOT=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)        DRY_RUN=true ;;
        --force)          FORCE=true ;;
        --confirm-reboot) CONFIRM_REBOOT=true ;;
        *) die "unknown option: $1 (--dry-run|--force|--confirm-reboot)" ;;
    esac
    shift
done

# The verdict lands here rather than in the exit code: an unrecoverable board is
# an expected outcome, and a unit stuck in `failed` forever would poison the
# "zero failed units" check this project uses as its health signal everywhere.
#
# The live verdict goes to /run and is rewritten every minute, so the UI always
# reports what is true now — including "ok" once a fault has cleared.
#
# Events that needed a human are *also* kept in STATE_DIR, and never overwritten
# by an ordinary tick. /run is tmpfs and journald is Storage=volatile on both
# boards, so a reboot erases every trace of why it happened; this line is what
# is left to read afterwards. Only notable verdicts are written, and only when
# they differ, so the SD card sees a few writes per fault, not one a minute.
status() {
    local line prev
    line="$(date -Iseconds) $*"
    mkdir -p "$RUN_DIR" 2>/dev/null || true
    printf '%s\n' "$line" > "$STATUS_FILE" 2>/dev/null || true

    case "$*" in
        STUCK:*|rebooting:*|recovered:*) ;;
        *) return 0 ;;
    esac
    prev="$(cut -d' ' -f2- "$STATUS_PERSIST" 2>/dev/null || true)"
    # Compared with the digits taken out: the verdicts embed the fault score,
    # which moves by a point or two every tick while a controller stays wedged
    # — and an expander board with escalation off sits like that for hours — so
    # a literal compare rewrote this SD-backed file once a minute.
    [[ "${prev//[0-9]/}" == "${*//[0-9]/}" ]] && return 0
    mkdir -p "$STATE_DIR" 2>/dev/null || true
    printf '%s\n' "$line" > "$STATUS_PERSIST" 2>/dev/null || true
}

# What the previous run concluded. Read back from the volatile copy, which is
# rewritten every tick, so this is genuinely "last minute" and never survives a
# reboot into a stale opinion.
previous_verdict() { cut -d' ' -f2- "$STATUS_FILE" 2>/dev/null || true; }

# --- detection --------------------------------------------------------------

# Weighted score of this adapter's faults in the window. See the header for why
# the weights differ. Prints an integer, always.
kernel_score() {
    local hci="$1" lines hw op tx
    [[ -n "$hci" ]] || { echo 0; return 0; }
    # -b: within this boot. Without it, a box with persistent journald re-counts
    # the pre-reboot evidence after the escalation reboot and goes straight
    # round again.
    lines="$(journalctl -k -b --since "-${WINDOW_SEC}s" --no-pager 2>/dev/null \
             | grep -F "Bluetooth: ${hci}: " || true)"
    [[ -n "$lines" ]] || { echo 0; return 0; }
    hw="$(grep -cE 'hardware error' <<<"$lines" || true)"
    op="$(grep -cE 'Opcode 0x0c03 failed' <<<"$lines" || true)"
    tx="$(grep -cE 'command .* tx timeout|command tx timeout' <<<"$lines" || true)"
    echo $(( 3 * ${hw:-0} + 2 * ${op:-0} + ${tx:-0} ))
}

# True only when the adapter is definitely not up. "unknown" — hciconfig gone
# after an apt upgrade, say — is not evidence of anything, and must never read
# as a wedge: the recovery for a wedge is a driver unbind or a reboot.
adapter_down() {
    [[ "$(bt_hci_state "${1:-}")" == "down" ]]
}

# Scoped to the adapter being policed. A global scan meant that soft-blocking an
# unused onboard radio silently disabled the watchdog for the dongle carrying
# audio — and images ship Bluetooth soft-blocked, so that state is not exotic.
rfkill_blocked() {
    local hci="${1:-}" d found=false
    if [[ -n "$hci" ]]; then
        for d in /sys/class/bluetooth/"${hci}"/rfkill*; do
            [[ -e "${d}/soft" ]] || continue
            found=true
            [[ "$(cat "${d}/soft" 2>/dev/null)" == "1" || "$(cat "${d}/hard" 2>/dev/null)" == "1" ]] \
                && return 0
        done
    fi
    $found && return 1
    # No per-adapter node (older kernels, or the adapter is gone): fall back to
    # the coarse scan rather than assuming unblocked.
    for d in /sys/class/rfkill/*; do
        [[ -e "${d}/type" ]] || continue
        [[ "$(cat "${d}/type" 2>/dev/null)" == "bluetooth" ]] || continue
        [[ "$(cat "${d}/soft" 2>/dev/null)" == "1" || "$(cat "${d}/hard" 2>/dev/null)" == "1" ]] \
            && return 0
    done
    return 1
}

# Is there a second, working controller? The documented workaround for the bad
# onboard chip is a USB dongle, and on such a box the audio is on the dongle —
# rebooting the machine because the unused onboard radio wedged would take out
# playback that was working.
other_adapter_up() {
    local mine="${1:-}" d name
    for d in /sys/class/bluetooth/hci*; do
        [[ -e "$d" ]] || continue
        name="$(basename "$d")"
        # hciN:M is an ACL connection, not an adapter. Counting one as a second
        # radio made this guard fire on a box that has only the onboard chip —
        # withholding the escalation on the one board that needs it. Seen live.
        [[ "$name" =~ ^hci[0-9]+$ ]] || continue
        [[ "$name" == "$mine" ]] && continue
        [[ "$(bt_hci_state "$name")" == "up" ]] && return 0
    done
    return 1
}

bluetoothd_settling() {
    local mono now_us up=""
    mono="$(systemctl show -p ActiveEnterTimestampMonotonic --value bluetooth.service 2>/dev/null || echo 0)"
    [[ "$mono" =~ ^[0-9]+$ ]] && (( mono > 0 )) || return 1
    # Microseconds in bash, not awk: these boards run 32-bit userland, where an
    # awk printf "%d" can be limited to a C long — 2^31 us is 36 minutes of
    # uptime, after which a clamped "now" sits below any later bluetoothd start
    # and the settle guard quietly stops applying. Bash arithmetic is 64-bit.
    read -r up _ < /proc/uptime 2>/dev/null || return 1
    up="${up%%.*}"
    [[ "$up" =~ ^[0-9]+$ ]] || return 1
    now_us=$(( up * 1000000 ))
    (( now_us > mono )) || return 1
    (( now_us - mono < SETTLE_SEC * 1000000 ))
}

# --- persistent accounting --------------------------------------------------
#
# One helper for the whole load/prune/append cycle, and one error policy. Two
# copies of this is how the cap came to fail open in the first place.
# Prints "<count>" on success, "ERR" when the ledger cannot be accounted for.
#
# Each entry records why it was written. The kernel evidence lives in journald,
# which is volatile here — destroyed by the reboot the entry is recording — so
# without the reason a capped-out box is three anonymous numbers.
ledger() {
    python3 - "$STATE_FILE" "${1:-count}" "$(clock_trusted && echo yes || echo no)" "${2:-}" <<'PY' 2>/dev/null || echo ERR
import json, os, sys, time, tempfile

path, action, clock_ok, reason = sys.argv[1], sys.argv[2], sys.argv[3] == "yes", sys.argv[4]
now = time.time()

data = {}
if os.path.exists(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except Exception:
        # Unreadable is NOT the same as empty: saying "zero reboots so far"
        # here would hand back an unlimited budget precisely when the state is
        # untrustworthy. Report the error and let the caller refuse.
        print("ERR")
        raise SystemExit


def stamp(entry):
    if isinstance(entry, (int, float)):          # legacy: a bare epoch
        return float(entry), {"t": float(entry), "reason": "(not recorded)"}
    if isinstance(entry, dict) and isinstance(entry.get("t"), (int, float)):
        return float(entry["t"]), entry
    return None, None


entries = []
for raw in data.get("reboots", []) or []:
    t, e = stamp(raw)
    if t is not None:
        entries.append((t, e))

# Pruning is only as sound as the clock. This board has no RTC: systemd restores
# a saved time at boot and NTP corrects it later — sometimes much later, since
# the box often has no route out. A forward step of more than a day would age
# every entry out and hand back a full budget at the exact moment the state is
# least trustworthy, so while the clock is unverified nothing is pruned.
if clock_ok:
    kept = [(t, e) for t, e in entries if t > now - 86400 or t > now]
else:
    kept = entries[-64:]

if action == "append":
    kept.append((now, {"t": now, "reason": reason or "(not recorded)",
                       "boot": open("/proc/sys/kernel/random/boot_id").read().strip()
                       if os.path.exists("/proc/sys/kernel/random/boot_id") else ""}))
    data["reboots"] = [e for _, e in kept]
    # Atomic + fsync: this is written milliseconds before a reboot, on a box
    # whose whole design assumes power can vanish mid-write.
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dirfd = os.open(d, os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        print("ERR")
        raise SystemExit

print(len(kept))
PY
}

# Has anything vouched for the wall clock this boot? systemd-timesyncd says so
# once it has heard from a server; without that the clock is whatever was saved
# at the last shutdown.
clock_trusted() {
    local v
    v="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)"
    [[ "$v" == "yes" ]]
}

# Attempt timestamps live beside the ledger in STATE_DIR, not in tmpfs: a
# cooldown erased by the very reboot it is meant to rate-limit is no cooldown.
# Format: "<boot_id> <uptime_seconds> <epoch>".
LAST_ATTEMPT="${STATE_DIR}/bt_wedge_last_attempt"

boot_id()  { cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo unknown; }
uptime_s() { awk '{printf "%d", $1}' /proc/uptime 2>/dev/null || echo 0; }

# Within a boot the cooldown is monotonic, so a late NTP step cannot touch it.
# Across boots there is nothing but the clock, so it is used with a guard: a
# backwards step used to make `now - last` negative, which is always less than
# the cooldown, freezing the watchdog permanently. An early attempt costs
# nothing — the 24 h reboot cap is the guard that must fail closed, and does.
in_cooldown() {
    local boot up epoch now
    [[ -f "$LAST_ATTEMPT" ]] || return 1
    read -r boot up epoch < "$LAST_ATTEMPT" 2>/dev/null || return 1
    if [[ "$boot" == "$(boot_id)" && "$up" =~ ^[0-9]+$ ]]; then
        (( $(uptime_s) - up < COOLDOWN_SEC ))
        return
    fi
    [[ "$epoch" =~ ^[0-9]+$ ]] || return 1
    now="$(date +%s)"
    (( now < epoch )) && return 1
    (( now - epoch < COOLDOWN_SEC ))
}

# Save what the logs said, before a reboot erases them. journald is volatile on
# these boards, so without this every wedge reboot destroys its own evidence and
# all that is left is a score in the ledger. Written before the watchdog's own
# reboot, and once per boot when a STUCK verdict leaves the reboot to a human
# (save_stuck_evidence): that reboot erases the logs just the same. The newest
# EVIDENCE_KEEP files are kept. Returns non-zero when nothing was saved; every
# caller ignores that.
# Root-owned, under STATE_DIR: this runs as root, and /var/tmp is world-writable,
# so a planted symlink there could aim the write or the cleanup at any file.
# Every collector is bounded by `timeout`: on the reboot path this runs after the
# ledger has been charged, in a oneshot unit with no start timeout, so one hung
# vcgencmd or journalctl would spend a reboot slot, never reboot, and stop the
# timer from ever firing again.
EVIDENCE_DIR="${EVIDENCE_DIR:-${STATE_DIR}/btwedge}"
EVIDENCE_KEEP="${EVIDENCE_KEEP:-10}"
# Checked up front: under `set -u` a non-numeric value is a fatal expansion error
# inside save_evidence (after the ledger is charged, before the reboot), and 0
# would delete the file just written.
[[ "$EVIDENCE_KEEP" =~ ^[1-9][0-9]*$ ]] || EVIDENCE_KEEP=10
save_evidence() {
    local reason="$1" hci="${2:-}" f kern
    [[ -L "$EVIDENCE_DIR" ]] && return 1
    install -d -m 0700 -o root -g root "$EVIDENCE_DIR" 2>/dev/null || return 1
    [[ -O "$EVIDENCE_DIR" && ! -L "$EVIDENCE_DIR" ]] || return 1
    # mktemp creates the file itself (O_EXCL), so nothing pre-made is followed.
    f="$(mktemp "${EVIDENCE_DIR}/wedge-$(date +%Y%m%d-%H%M%S).XXXXXX.log" 2>/dev/null)" || return 1
    # The kernel log for the whole boot, not the last ten minutes: the open
    # theory has a wedge following a network-adapter hot-swap by 9-51 min, a
    # reboot can come long after the onset (a pair in flight, a cooldown, a
    # STUCK box rebooted by hand), and --since is wall-clock, so an NTP step on
    # this RTC-less board hides everything before it. Filtered, it is small;
    # only the tx timeouts, which repeat every 2 s for as long as a wedge lasts,
    # are summarised so they cannot push the onset out of the tail.
    kern="$(timeout 20 journalctl -k -b --no-pager -o short-monotonic 2>&1 \
            | grep -iE 'bluetooth|hci|usb|voltage|brcm|bcm|uart|serial' || true)"
    {
        echo "reason: ${reason}"
        echo "date:   $(date -Is)   boot: $(boot_id)"
        echo "uptime: $(cut -d' ' -f1 /proc/uptime 2>/dev/null) s   $(timeout 5 vcgencmd get_throttled 2>/dev/null)"
        # Plain hciconfig: the kernel's own flags and counters. -a also sends
        # HCI commands (read name, class, version) to the controller — the
        # traffic d924cf1 stopped sending, a 1 s timeout each on a wedged chip —
        # and lists every adapter, so a cut could drop the one being policed.
        echo; echo "== hciconfig ${hci:-(all adapters)}"
        timeout 5 hciconfig ${hci:+"$hci"} 2>&1 | head -40
        echo; echo "== bt_status.json"; cat "$BT_STATUS_FILE" 2>&1
        echo; echo "== kernel, this boot (Bluetooth / USB / power), tx timeouts summarised below"
        grep -v 'tx timeout' <<<"$kern" | tail -300
        echo; echo "== kernel tx timeouts this boot: $(grep -c 'tx timeout' <<<"$kern"), the last 10"
        grep 'tx timeout' <<<"$kern" | tail -10
        echo; echo "== services, last 10 min (the watchdog's own runs included)"
        timeout 20 journalctl -b --since "-600s" --no-pager -o short-monotonic \
            -u bluetooth -u bluealsa -u bluealsa-aplay -u audio-player-mpv -u audio-player-backend \
            -u audio-player-bt-retry -u audio-player-bt-autoconnect -u audio-player-bt-watchdog 2>&1 \
            | grep -vE ' A: [0-9]|Adv Monitor|Path / reserved|GET /api/' | tail -400
    } > "$f" 2>/dev/null || true
    # Empty means the write failed (a full or read-only card): say so, and do
    # not let an empty file push a real one out of the rotation.
    if [[ ! -s "$f" ]]; then
        rm -f -- "$f"
        warn "could not save the wedge logs to ${EVIDENCE_DIR}"
        return 1
    fi
    sync -f "$f" 2>/dev/null || sync
    find "$EVIDENCE_DIR" -maxdepth 1 -type f -name 'wedge-*.log' -printf '%T@ %p\0' 2>/dev/null \
        | sort -zrn | tail -zn +"$((EVIDENCE_KEEP + 1))" | cut -zd' ' -f2- | xargs -0r rm -f --
    log "saved the wedge logs to ${f}"
}

# For a STUCK verdict that leaves the reboot to a human ("sudo systemctl
# reboot"), which erases the logs as surely as ours does — and a capped-out box
# lands here on its next wedge. Once per boot: a STUCK board is re-judged every
# minute, sometimes for hours, and the onset is the part worth keeping. The
# marker sits in the root-only evidence directory.
save_stuck_evidence() {
    local mark="${EVIDENCE_DIR}/.saved-boot" b
    b="$(boot_id)"
    [[ ! -L "$mark" && "$(cat "$mark" 2>/dev/null)" == "$b" ]] && return 0
    save_evidence "$@" || return 0
    printf '%s\n' "$b" > "$mark" 2>/dev/null || true
}

mark_attempt() {
    mkdir -p "$STATE_DIR" 2>/dev/null || true
    printf '%s %s %s\n' "$(boot_id)" "$(uptime_s)" "$(date +%s)" > "$LAST_ATTEMPT" 2>/dev/null || true
}

wedge_reboot_enabled() {
    local flag
    flag="$(cfg_get bluetooth.wedge_reboot 2>/dev/null || echo false)"
    flag="${flag//\'/}"
    [[ "${flag,,}" == "true" || "${flag}" == "1" ]]
}

# --- main -------------------------------------------------------------------

main() {
    [[ "${EUID}" -eq 0 ]] || die "run as root: sudo $0"

    systemctl is-active bluetooth.service >/dev/null 2>&1 || exit 0

    if [[ ! -d "$DRIVER_DIR" ]]; then
        $DRY_RUN && log "no serdev Bluetooth controller here — watchdog does not apply"
        status "n/a: no serdev controller"
        exit 0
    fi

    # Empty is a meaningful answer: the driver is bound but no adapter belongs
    # to it. Falling back to hci0 there read a USB dongle instead and reported
    # the box healthy — or, after a "successful" rebind, reported the dongle's
    # health as the onboard chip's recovery.
    local hci
    hci="$(bt_serdev_hci || true)"

    if rfkill_blocked "$hci"; then
        $DRY_RUN && log "Bluetooth is rfkill-blocked — switched off, not wedged"
        status "idle: rfkill blocked"
        exit 0
    fi

    if bluetoothd_settling; then
        $DRY_RUN && log "bluetoothd restarted within ${SETTLE_SEC}s — letting the adapter settle"
        status "idle: bluetoothd settling"
        exit 0
    fi

    local score=0 reason=""
    if [[ -z "$hci" ]]; then
        # Confirmed twice like the DOWN case, and for the same reason: this is
        # the strongest trigger there is, and the adapter is briefly absent
        # while the driver re-registers it after any recovery.
        if $DRY_RUN; then
            reason="the serdev adapter is missing (unconfirmed in dry-run)"
        else
            sleep "$CONFIRM_DOWN_SEC"
            [[ -z "$(bt_serdev_hci || true)" ]] \
                && reason="the serdev adapter has vanished while its driver is still bound"
        fi
    else
        score="$(kernel_score "$hci")"
        [[ "$score" =~ ^[0-9]+$ ]] || score=0
        if (( score >= EVENT_THRESHOLD )); then
            reason="controller fault score ${score}/${EVENT_THRESHOLD} in the kernel log in ${WINDOW_SEC}s"
        elif adapter_down "$hci"; then
            # One sample is not evidence. bt_clear_endpoint_cache restarts
            # bluetoothd, and for a second or two afterwards the service is
            # active while the adapter is not yet powered — reading that once
            # and going straight to a driver unbind would tear the controller
            # down mid-pair.
            if $DRY_RUN; then
                reason="${hci} reads down (unconfirmed in dry-run)"
            else
                sleep "$CONFIRM_DOWN_SEC"
                adapter_down "$hci" && reason="${hci} is down on two reads ${CONFIRM_DOWN_SEC}s apart"
            fi
        fi
    fi

    if $FORCE; then
        reason="${reason:-forced}"
    elif [[ -z "$reason" ]]; then
        # Hysteresis. A controller can fault just often enough to sit on the
        # threshold — one timeout every ~40 s puts the 100 s score at 2 or 3
        # depending on when the tick lands — and a verdict that alternates
        # between "healthy" and "STUCK" every minute is worse than either: the
        # UI flickers and nobody trusts it. Having said it is faulty, keep
        # saying so until a window comes back completely clean.
        local prev
        prev="$(previous_verdict)"
        if (( score > 0 )) && [[ "$prev" == STUCK:* || "$prev" == waiting:* ]]; then
            $DRY_RUN && log "still faulting intermittently: score ${score} in ${WINDOW_SEC}s"
            status "STUCK: faulting intermittently, score ${score}/${EVENT_THRESHOLD} in ${WINDOW_SEC}s"
            exit 0
        fi
        $DRY_RUN && log "healthy: fault score ${score} in ${WINDOW_SEC}s, ${hci:-adapter} $(bt_hci_state "${hci:-}")"
        status "ok: healthy"
        exit 0
    fi

    # Never act while the user is pairing, connecting or scanning: unbinding the
    # driver underneath that looks exactly like the operation failing. Detection
    # happens first, though — it is read-only, and reporting "idle" for a box
    # whose radio is dead is how a real wedge stayed invisible in the UI while
    # bt-retry hammered away at it.
    if ! $FORCE && bt_ops_busy; then
        $DRY_RUN && log "WEDGED: ${reason} — standing down, a pair/connect/scan is in flight"
        status "waiting: ${reason} (a bluetooth operation is in flight)"
        exit 0
    fi

    local verdict can_rebind=false
    verdict="$(bt_rebind_verdict)"
    [[ "$verdict" == "yes" ]] && can_rebind=true

    if $DRY_RUN; then
        log "WEDGED: ${reason}"
        case "$verdict" in
            yes) log "[dry-run] would rebind the driver (BT_ON is a real SoC GPIO here)" ;;
            no)  log "[dry-run] would NOT rebind — BT_ON is on the VideoCore expander, where that kills a healthy controller" ;;
            *)   log "[dry-run] would NOT rebind — cannot establish whether a rebind power-cycles this chip (install gpiod for the precise probe)" ;;
        esac
        wedge_reboot_enabled \
            && log "[dry-run] reboot escalation is enabled" \
            || log "[dry-run] reboot escalation is disabled (bluetooth.wedge_reboot)"
        other_adapter_up "$hci" && log "[dry-run] another adapter is up — would not reboot regardless"
        exit 0
    fi

    # Nothing to try: the rebind is refused on this board and the reboot is
    # switched off. Say so and stop, rather than burning a cooldown slot and an
    # SD write on an "attempt" that does not exist. A board can sit here for
    # hours, and the verdict flapping between waiting and STUCK every five
    # minutes told the UI a different story each time.
    if ! $can_rebind && ! wedge_reboot_enabled; then
        warn "Bluetooth controller wedged: ${reason}"
        case "$verdict" in
            no) warn "A rebind cannot recover this board: BT_ON is on the VideoCore expander." ;;
            *)  warn "Cannot establish whether a rebind recovers this board (install gpiod)." ;;
        esac
        warn "Reboot escalation is off (web UI → Settings → Bluetooth). To recover now:"
        warn "  sudo systemctl reboot"
        save_stuck_evidence "$reason" "$hci" || true
        status "STUCK: unrecoverable without a reboot, escalation disabled (${reason})"
        exit 0
    fi

    # Reboot is the only remedy here and today's budget is already spent: say
    # STUCK on every tick. Checked before the cooldown because the cap path
    # marks an attempt too, so the next five ticks used to report "waiting:
    # cooldown" — and the UI read that as "the box will reboot by itself",
    # which it will not until the oldest reboot ages out of the 24 h window.
    # Read-only (ledger count), so it costs no attempt and no SD write.
    if ! $FORCE && ! $can_rebind; then
        local spent
        spent="$(ledger count)"
        if [[ "$spent" =~ ^[0-9]+$ ]] && (( spent >= REBOOT_MAX_PER_DAY )); then
            log "wedge detected (${reason}); reboot cap already reached (${spent}/${REBOOT_MAX_PER_DAY} in 24h)"
            save_stuck_evidence "$reason" "$hci" || true
            status "STUCK: reboot cap reached (${spent}/${REBOOT_MAX_PER_DAY} in 24h)"
            exit 0
        fi
    fi

    if ! $FORCE && in_cooldown; then
        log "wedge detected (${reason}) but within ${COOLDOWN_SEC}s cooldown — leaving it"
        status "waiting: cooldown after a recent attempt"
        exit 0
    fi

    # Checked again here: between the check above and this point lie the
    # cooldown read and, below, a ~26 s recovery. A user can tap Pair in that
    # window, and the tear-down would look to them exactly like it failing.
    if ! $FORCE && bt_ops_busy; then
        log "a pair/connect/scan started while confirming — standing down"
        status "waiting: ${reason} (a bluetooth operation is in flight)"
        exit 0
    fi
    mark_attempt

    warn "Bluetooth controller wedged: ${reason}"

    if $can_rebind; then
        log "recovering by rebinding the driver"
        local rc=0
        "${SCRIPT_DIR}/bt_recover.sh" || rc=$?
        if (( rc == 0 )); then
            log "controller recovered — bt-retry.timer reconnects within 45s"
            status "recovered: rebind"
            exit 0
        elif (( rc == 2 )); then
            # It refused. Both read the same verdict function, so this means the
            # board answered differently between the two calls — worth saying
            # plainly rather than reporting it as a failed recovery.
            warn "bt_recover.sh refused the rebind — it read this board differently than we did"
        else
            warn "rebind did not bring the controller back"
        fi
    elif [[ "$verdict" == "no" ]]; then
        warn "skipping the rebind: BT_ON is on the VideoCore expander on this board,"
        warn "where unbinding cannot power-cycle the chip and kills it even when healthy"
    else
        warn "skipping the rebind: cannot establish whether unbinding power-cycles the"
        warn "chip on this board. Install gpiod for the precise probe; refusing to guess"
        warn "because guessing wrong costs a working controller."
    fi

    if ! wedge_reboot_enabled; then
        warn "reboot escalation is off. Turn it on in the web UI (Settings → Bluetooth)"
        warn "if you want the box to recover unattended; until then, to recover now:"
        warn "  sudo systemctl reboot"
        save_stuck_evidence "$reason" "$hci" || true
        status "STUCK: unrecoverable without a reboot, escalation disabled (${reason})"
        exit 0
    fi

    if other_adapter_up "$hci"; then
        warn "another Bluetooth adapter is up — not rebooting the machine for this one."
        warn "Audio on a box with a USB dongle runs over the dongle; a reboot would take"
        warn "out playback that is working. Recover the onboard controller with a reboot"
        warn "at a time of your choosing:  sudo systemctl reboot"
        save_stuck_evidence "$reason" "$hci" || true
        status "STUCK: second adapter healthy, reboot withheld (${reason})"
        exit 0
    fi

    if $FORCE && ! $CONFIRM_REBOOT; then
        warn "--force will not reboot the machine. Re-run with --confirm-reboot if that is what you want."
        status "STUCK: forced run declined to reboot"
        exit 0
    fi

    local n
    n="$(ledger count)"
    if [[ "$n" == "ERR" ]]; then
        warn "cannot read the reboot ledger (${STATE_FILE}) — refusing to escalate."
        warn "Failing closed on purpose: an unreadable ledger is an unlimited reboot budget."
        save_stuck_evidence "$reason" "$hci" || true
        status "STUCK: reboot ledger unreadable, escalation refused"
        exit 0
    fi
    if (( n >= REBOOT_MAX_PER_DAY )); then
        warn "already rebooted ${n} times in 24h for this fault — refusing to loop."
        warn "The board stays up and broken on purpose, so it can be diagnosed:"
        warn "  ${STATE_FILE}  records what each reboot was for"
        warn "  ${EVIDENCE_DIR}  holds the logs saved at each reboot and at this wedge"
        save_stuck_evidence "$reason" "$hci" || true
        status "STUCK: reboot cap reached (${n}/${REBOOT_MAX_PER_DAY} in 24h)"
        exit 0
    fi

    if [[ "$(ledger append "$reason")" == "ERR" ]]; then
        warn "could not record the reboot — refusing to escalate rather than lose the cap."
        save_stuck_evidence "$reason" "$hci" || true
        status "STUCK: could not write the reboot ledger"
        exit 0
    fi

    warn "rebooting to recover the controller ($((n + 1))/${REBOOT_MAX_PER_DAY} in the last 24h)"
    save_evidence "$reason" "$hci" || true
    status "rebooting: ${reason}"
    sync
    # --no-block: this runs inside a systemd unit that the reboot job must stop,
    # so a blocking call can deadlock against its own shutdown.
    systemctl --no-block reboot
}

main "$@"

#!/usr/bin/env bash
# USB auto-mount (vfat/exfat/ntfs/ext4) → /media/usb + backend rescan
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

USB_MOUNT="${USB_MOUNT:-/media/usb}"
STATE_FILE="${STATE_DIR}/usb_mount.json"
# NOT SUDO_USER: udev runs this as root with SUDO_USER unset, so the same stick
# would be owned differently depending on whether udev or a human mounted it.
# The config key is the one answer both paths agree on.
PI_USER="${PI_USER:-$(cfg_get paths.owner 2>/dev/null || true)}"
PI_USER="${PI_USER:-pi}"
if id -u "$PI_USER" &>/dev/null; then
    PI_UID="$(id -u "$PI_USER")"
    PI_GID="$(id -g "$PI_USER")"
else
    PI_UID="${PI_UID:-1000}"
    PI_GID="${PI_GID:-1000}"
fi

acquire_lock() {
    local lock="${RUN_DIR}/usb_automount.lock"
    mkdir -p "$RUN_DIR"
    exec 8>"$lock"
    flock -w 10 8 || die "automount busy"
}

usage() {
    cat <<'EOF'
Usage: usb_automount.sh <add|remove|mount|unmount> [DEVICE]

Mounts the first suitable USB partition to /media/usb and triggers
/api/usb/scan. Called from udev on block device add/remove.
EOF
}

write_state() {
    local mounted="$1" dev="${2:-}" fstype="${3:-}"
    mkdir -p "$STATE_DIR"
    # Values go in as argv, never spliced into the source: the old heredoc
    # produced `"mounted": true`, a NameError in Python, so under set -e every
    # mount and unmount died here — before the rescan and the cache clear.
    python3 - "$STATE_FILE" "$mounted" "$dev" "$fstype" "$USB_MOUNT" <<'PY'
import json, os, sys
from datetime import datetime, timezone

path, mounted, dev, fstype, mount_point = sys.argv[1:6]
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump({
        "mounted": mounted == "true",
        "device": dev or None,
        "fstype": fstype or None,
        "mount_point": mount_point,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, fh, indent=2)
    fh.write("\n")
    fh.flush()
    os.fsync(fh.fileno())
os.replace(tmp, path)
PY
}

# --mountpoint, not --target: --target reports the mount that *contains* the
# path, so with nothing on /media/usb it answered with the SD card's root and
# "already mounted" was always true — the stick was never mounted.
is_mounted() {
    mountpoint -q "$USB_MOUNT" 2>/dev/null
}

mounted_source() {
    findmnt -n -o SOURCE --mountpoint "$USB_MOUNT" 2>/dev/null || true
}

candidate_partitions() {
    local dev
    for dev in /dev/disk/by-id/usb-*; do
        [[ -e "$dev" ]] || continue
        if [[ -b "$dev" ]]; then
            readlink -f "$dev"
            continue
        fi
        local part
        for part in "${dev}-part"*; do
            [[ -e "$part" ]] || continue
            readlink -f "$part"
        done
    done
}

pick_device() {
    local want="${1:-}"
    local dev
    if [[ -n "$want" && -b "$want" ]]; then
        echo "$want"
        return 0
    fi
    while IFS= read -r dev; do
        [[ -n "$dev" && -b "$dev" ]] || continue
        if findmnt -n --source "$dev" >/dev/null 2>&1; then
            continue
        fi
        echo "$dev"
        return 0
    done < <(candidate_partitions | sort -u)
    return 1
}

mount_device() {
    local dev="$1"
    require_cmd blkid mount mkdir
    local fstype uuid label
    fstype="$(blkid -o value -s TYPE "$dev" 2>/dev/null || true)"
    uuid="$(blkid -o value -s UUID "$dev" 2>/dev/null || true)"
    label="$(blkid -o value -s LABEL "$dev" 2>/dev/null || true)"

    mkdir -p "$USB_MOUNT"
    if is_mounted; then
        log "already mounted: $(mounted_source) → ${USB_MOUNT}"
        write_state true "$(mounted_source)" "$(findmnt -n -o FSTYPE --mountpoint "$USB_MOUNT" 2>/dev/null || true)"
        trigger_rescan
        return 0
    fi

    # nosuid,nodev,noexec: this is a root mount of whatever stick gets plugged
    # in. An ext4 stick can carry a setuid-root binary or a device node, and a
    # music library never needs either — the same defaults udisks applies.
    local -a opts=(noatime nosuid nodev noexec)
    case "${fstype,,}" in
        vfat|msdos|fat|fat32)
            opts+=(uid="${PI_UID}",gid="${PI_GID}",utf8,shortname=mixed)
            mount -t vfat -o "$(IFS=,; echo "${opts[*]}")" "$dev" "$USB_MOUNT"
            ;;
        exfat)
            opts+=(uid="${PI_UID}",gid="${PI_GID}")
            mount -t exfat -o "$(IFS=,; echo "${opts[*]}")" "$dev" "$USB_MOUNT"
            ;;
        ntfs)
            command -v ntfs-3g >/dev/null 2>&1 || die "ntfs-3g required for NTFS USB"
            opts+=(uid="${PI_UID}",gid="${PI_GID}")
            mount -t ntfs-3g -o "$(IFS=,; echo "${opts[*]}")" "$dev" "$USB_MOUNT"
            ;;
        ext4|ext3|ext2)
            mount -o "$(IFS=,; echo "${opts[*]}")" "$dev" "$USB_MOUNT"
            ;;
        "")
            mount -o "$(IFS=,; echo "${opts[*]}")" "$dev" "$USB_MOUNT"
            fstype="$(findmnt -n -o FSTYPE --mountpoint "$USB_MOUNT" 2>/dev/null || echo auto)"
            ;;
        *)
            warn "unknown fstype ${fstype} — trying generic mount"
            mount -o "$(IFS=,; echo "${opts[*]}")" "$dev" "$USB_MOUNT"
            ;;
    esac

    log "mounted ${dev} (${fstype:-auto}${label:+ label=${label}}) → ${USB_MOUNT}"
    write_state true "$dev" "${fstype:-auto}"
    trigger_rescan
}

unmount_usb() {
    if ! is_mounted; then
        write_state false
        clear_cache
        return 0
    fi
    local src
    src="$(mounted_source)"
    umount -l "$USB_MOUNT" 2>/dev/null || umount "$USB_MOUNT" 2>/dev/null || true
    log "unmounted ${src} from ${USB_MOUNT}"
    write_state false
    clear_cache
    trigger_rescan
}

clear_cache() {
    rm -f "${STATE_DIR}/usb_tracks.json" 2>/dev/null || true
}

trigger_rescan() {
    if command -v curl >/dev/null 2>&1; then
        curl -sf -m 15 -X POST http://127.0.0.1/api/usb/scan >/dev/null 2>&1 \
            || curl -sf -m 15 -X POST http://127.0.0.1:8000/api/usb/scan >/dev/null 2>&1 \
            || warn "USB rescan API call failed (backend down?)"
    fi
}

# udev runs RUN+= programs inside systemd-udevd's sandbox, and three parts of it
# defeat this script: a private mount namespace (PrivateMounts=yes), so a mount
# made here is visible to no other process — not the backend, not mpv;
# IPAddressDeny=any, so the rescan POST to 127.0.0.1 is refused; and anything
# still running when the event ends (the ntfs-3g FUSE daemon) is killed. So when
# udev is the caller, hand the work to a transient systemd service, which runs
# in the host's namespace. KillMode=process keeps a FUSE daemon alive after the
# script itself exits.
detach_from_udev() {
    [[ -n "${SEQNUM:-}" && -n "${DEVPATH:-}" && -z "${SAFYDO_DETACHED:-}" ]] || return 0
    command -v systemd-run >/dev/null 2>&1 || return 0
    exec systemd-run --no-block --quiet --collect \
        --property=KillMode=process \
        --setenv=SAFYDO_DETACHED=1 \
        "${SCRIPT_DIR}/$(basename "$0")" "$@"
}

main() {
    local action="${1:-}"
    local dev="${2:-}"

    [[ "${EUID}" -eq 0 ]] || die "run as root: sudo $0 ..."
    detach_from_udev "$@"
    acquire_lock

    case "$action" in
        add|mount)
            if dev="$(pick_device "$dev")"; then
                mount_device "$dev"
            else
                warn "no USB partition found to mount"
            fi
            ;;
        remove|unmount)
            if [[ -n "$dev" ]]; then
                local src
                src="$(readlink -f "$dev" 2>/dev/null || echo "$dev")"
                if [[ "$(mounted_source)" == "$src" ]]; then
                    unmount_usb
                    exit 0
                fi
            fi
            if is_mounted; then
                local src_base
                src_base="$(mounted_source)"
                # Whole name, not a substring: removing sda1 must not unmount
                # an sda10 that happens to be the one mounted.
                if [[ -z "$dev" ]] || [[ "${src_base##*/}" == "${dev##*/}" ]]; then
                    unmount_usb
                fi
            else
                write_state false
                clear_cache
            fi
            ;;
        -h|--help)
            usage
            ;;
        *)
            usage
            exit 1
            ;;
    esac
}

main "$@"
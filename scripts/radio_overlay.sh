#!/usr/bin/env bash
# Add or remove the DABBoard block in config.txt. Takes effect at the next reboot.
# config.txt lives on FAT, so write a copy, sync, rename, sync: a power cut then
# leaves the old file or the new one, never half of one.
set -euo pipefail

BEGIN="# BEGIN SAFYDO RADIO"
END="# END SAFYDO RADIO"
CONFIG="/boot/firmware/config.txt"
[[ -f "$CONFIG" ]] || CONFIG="/boot/config.txt"
[[ -f "$CONFIG" ]] || { echo "config.txt not found" >&2; exit 1; }
[[ "${EUID}" -eq 0 ]] || { echo "radio_overlay.sh requires root" >&2; exit 1; }

present() { grep -qxF "$BEGIN" "$CONFIG"; }

replace_config() {
    sync
    mv "${CONFIG}.new" "$CONFIG"
    sync
}

case "${1:-}" in
    enable)
        present && exit 0
        cp -p "$CONFIG" "${CONFIG}.bak-radio"
        # A config.txt without a final newline would get the BEGIN marker glued
        # to its last line: `present` then never matches, so disable cannot
        # remove the block and every enable appends another one.
        {
            cat "$CONFIG"
            [[ -z "$(tail -c1 "$CONFIG")" ]] || echo
            cat
        } > "${CONFIG}.new" <<BLOCK
${BEGIN}
[pi3]
dtparam=spi=on
dtparam=i2s=on
dtoverlay=ugreen-dabboard
[all]
${END}
BLOCK
        replace_config
        ;;
    disable)
        present || exit 0
        cp -p "$CONFIG" "${CONFIG}.bak-radio"
        sed "/^${BEGIN}\$/,/^${END}\$/d" "$CONFIG" > "${CONFIG}.new"
        replace_config
        ;;
    status)
        present && echo enabled || echo disabled
        ;;
    *)
        echo "usage: $0 enable|disable|status" >&2
        exit 2
        ;;
esac

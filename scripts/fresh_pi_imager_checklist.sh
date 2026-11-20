#!/usr/bin/env bash
# Post-flash checklist — run from dev PC after Pi Imager flash.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# BOARD=zero2w (Zero 2 W + Bookworm) | pi3aplus (3 A+ + Trixie) — see docs/porting-pi3aplus.md
BOARD="${BOARD:-zero2w}"
case "$BOARD" in
    zero2w)   OS_NAME="Bookworm"; HOST_DEFAULT="safydo" ;;
    pi3aplus) OS_NAME="Trixie";   HOST_DEFAULT="safydo3a" ;;
    *) echo "Unknown BOARD=$BOARD (use zero2w or pi3aplus)" >&2; exit 1 ;;
esac
SAFYDO_HOSTNAME="${SAFYDO_HOSTNAME:-$HOST_DEFAULT}"
PI_HOST="${PI_HOST:-${SAFYDO_HOSTNAME}.local}"
PI_USER="${PI_USER:-${USER:-pi}}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"

echo "=== Target: ${BOARD} ==="
echo "=== Raspberry Pi Imager (before writing SD) ==="
echo "  OS: Raspberry Pi OS Lite, 32-bit, ${OS_NAME}"
echo "  Hostname: ${SAFYDO_HOSTNAME}   (two boards on one LAN must not share a name)"
echo "  User: ${PI_USER}"
echo "  SSH: Enable (public key below)"
if [[ "$BOARD" == "pi3aplus" ]]; then
    echo "  WiFi: leave unconfigured — boot with the USB ethernet dongle plugged in"
else
    echo "  WiFi: SAME network as this PC (must have internet)"
fi
echo ""
echo "=== Paste this public key into Imager ==="
if [[ -f "${SSH_KEY}.pub" ]]; then
    cat "${SSH_KEY}.pub"
else
    echo "  (missing ${SSH_KEY}.pub — generate: ssh-keygen -t ed25519)"
fi
echo ""
echo "=== After first boot — verify BEFORE install.sh ==="
echo "  ping -c2 ${PI_HOST}"
echo "  ssh -i ${SSH_KEY} ${PI_USER}@${PI_HOST} 'curl -sI https://google.com | head -1'"
echo ""
echo "=== Deploy (reboot-safe: NM-only WiFi + network_ready service) ==="
echo "  cd \"${REPO}\""
echo "  ./scripts/build_frontend.sh"
echo "  rsync -avz --exclude .git --exclude frontend/node_modules \\"
echo "    -e \"ssh -i ${SSH_KEY}\" ./ ${PI_USER}@${PI_HOST}:~/rpi-audio-player/"
echo "  ssh -i ${SSH_KEY} ${PI_USER}@${PI_HOST} \\"
echo "    'sudo SKIP_WIFI_TUNING=1 SKIP_FSTAB_TMPFS=1 rsync -a ~/rpi-audio-player/ /opt/rpi-audio-player/ && \\"
echo "     sudo SKIP_WIFI_TUNING=1 SKIP_FSTAB_TMPFS=1 SAFYDO_HOSTNAME=${SAFYDO_HOSTNAME} /opt/rpi-audio-player/install.sh'"
echo "  (or from the dev box: PI_HOST=<ip> SAFYDO_HOSTNAME=${SAFYDO_HOSTNAME} ./scripts/deploy.sh --build)"
echo "  install.sh logs the detected platform first — check it before anything else"
echo ""
echo "=== Pair your Bluetooth speaker ==="
echo "  ssh -i ${SSH_KEY} ${PI_USER}@${PI_HOST} \\"
echo "    'sudo /opt/rpi-audio-player/scripts/bt_pair.sh pair AA:BB:CC:DD:EE:FF'"
echo ""
echo "=== After streams work — reboot test ==="
echo "  ssh ... 'sudo reboot'"
echo "  wait 90s, then: curl -sI https://google.com | head -1"
if [[ "$BOARD" == "pi3aplus" ]]; then
    echo "  Overclock: NOT recommended here (already 1400 MHz, no heatspreader)"
    echo "  No USB gadget dev link on this board — GPIO serial (pins 6/8/10) is the fallback"
    echo "  Once the dongle is proven, drop onboard WiFi:"
    echo "    sudo /opt/rpi-audio-player/scripts/wifi_onboard.sh disable --confirm"
    echo "  Single USB port: dongle OR music stick — a powered hub is needed for both"
else
    echo "  Optional tuning (test after reboot): APPLY_OVERCLOCK=1 install.sh --skip-apt"
    echo "  USB dev link (dwc2 OTG gadget) installs by default; once verified, disable"
    echo "  onboard WiFi: sudo /opt/rpi-audio-player/scripts/wifi_onboard.sh disable --confirm"
fi
echo ""
echo "=== If SSH key missing on fresh image ==="
echo "  Use Pi Imager SSH key, or: ssh-copy-id with password once"
echo ""
echo "=== If SSH refuses the password you KNOW is right ==="
echo "  Look at the login banner. If it says:"
echo "    'SSH may not work until a valid user has been set up'"
echo "  then no user account exists — Imager's OS customisation never ran."
echo "  Since Bookworm there is no pre-created 'pi' user, so every password fails."
echo "  It is NOT a stale host key (that errors before the password prompt) and"
echo "  NOT PasswordAuthentication=no (check: ssh -v shows 'publickey,password')."
echo "  Fix headless — SD card into this PC, then:"
echo "    printf '${PI_USER}:%s\\n' \"\$(openssl passwd -6 'YOUR_PASSWORD')\" \\"
echo "      | sudo tee /media/\$USER/bootfs/userconf.txt"
echo "    sudo touch /media/\$USER/bootfs/ssh   # harmless if SSH is already on"
echo "  Boot, wait ~60s, log in, then ssh-copy-id. Alternative: HDMI + USB"
echo "  keyboard and complete the on-console first-boot wizard."
echo "  Prevention: in Imager answer YES to 'apply OS customisation settings',"
echo "  and fill the General tab (user!) — 'Enable SSH' alone is not enough."
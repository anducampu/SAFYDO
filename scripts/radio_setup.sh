#!/usr/bin/env bash
# Install the vendor's radio_cli and the WiringPi it links against.
#
# Neither ships with SAFYDO: the licence on radio_cli forbids redistribution, so
# you download your own copy here. It goes outside the install directory because
# a deploy prunes that with rsync --delete.
#
#   sudo scripts/radio_setup.sh --accept-licence [--zip /path/to/Files_v16.zip]
#
# No route out from the board: fetch the zip elsewhere, scp it over, pass --zip,
# and install WiringPi from a .deb the same way (--wiringpi-deb).
set -euo pipefail

BUNDLE_URL="https://ugreen.eu/wp-content/uploads/files/Files_v16.zip"
BUNDLE_SHA256="68fb363f04d29515d05d90a32066780beaf242eec926cc007b1de5b8539b4318"
WIRINGPI_VERSION="3.20"
WIRINGPI_SHA256_armhf="77b66d85109374f82ed5e1da03f7e710b3ec419fd9c39db10c0d66ca1542c055"
WIRINGPI_SHA256_arm64="85f5965d57adb895b97b3f8b04083613d00964f930cb86822459eae5a97dc804"
DEST="/usr/local/lib/ugreen-dab+"

zip_path=""
deb_path=""
accepted=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --accept-licence) accepted=true; shift ;;
        --zip) zip_path="$2"; shift 2 ;;
        --wiringpi-deb) deb_path="$2"; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

[[ "${EUID}" -eq 0 ]] || { echo "radio_setup.sh requires root" >&2; exit 1; }

arch="$(dpkg --print-architecture)"
case "$arch" in
    armhf) bits="32-bit"; wiringpi_sha="$WIRINGPI_SHA256_armhf" ;;
    arm64) bits="64-bit"; wiringpi_sha="$WIRINGPI_SHA256_arm64" ;;
    *) echo "unsupported architecture: $arch" >&2; exit 1 ;;
esac

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

check_sha() {
    echo "$2  $1" | sha256sum -c --quiet - || { echo "checksum mismatch: $1" >&2; exit 1; }
}

if [[ -z "$zip_path" ]]; then
    zip_path="${work}/bundle.zip"
    curl -fsSL -m 180 -o "$zip_path" "$BUNDLE_URL"
fi
check_sha "$zip_path" "$BUNDLE_SHA256"

unzip -q -d "$work" "$zip_path" "Files_v16/license.txt" "Files_v16/bin/${bits}/radio_cli_v3.2.1"
echo "--- vendor licence ---"
cat "${work}/Files_v16/license.txt"
echo "----------------------"
if ! $accepted; then
    echo "Read the licence above, then run again with --accept-licence." >&2
    exit 1
fi

if ! dpkg -s wiringpi >/dev/null 2>&1; then
    if [[ -z "$deb_path" ]]; then
        deb_path="${work}/wiringpi.deb"
        curl -fsSL -m 120 -o "$deb_path" \
            "https://github.com/WiringPi/WiringPi/releases/download/v${WIRINGPI_VERSION}/wiringpi_${WIRINGPI_VERSION}_${arch}.deb"
    fi
    check_sha "$deb_path" "$wiringpi_sha"
    dpkg -i "$deb_path"
    /sbin/ldconfig
fi

install -d -m 755 "$DEST"
install -m 755 "${work}/Files_v16/bin/${bits}/radio_cli_v3.2.1" "${DEST}/radio_cli"
install -m 644 "${work}/Files_v16/license.txt" "${DEST}/license.txt"

if ldd "${DEST}/radio_cli" | grep -q 'not found'; then
    ldd "${DEST}/radio_cli" | grep 'not found' >&2
    echo "radio_cli has unresolved libraries" >&2
    exit 1
fi
systemctl enable --now audio-player-radio.service 2>/dev/null || true
echo "radio_cli installed in ${DEST}. Turn the radio on in Settings, then reboot."

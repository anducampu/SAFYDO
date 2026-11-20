#!/usr/bin/env bash
# Download armhf .deb packages for offline install on Pi (dev machine).
# SUITE must match the Pi's OS release: bookworm (Zero 2W) or trixie (3 A+).
set -euo pipefail

SUITE="${SUITE:-bookworm}"
PI_HOST="${PI_HOST:-pi@safydo.local}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
PKG="${1:-mpv}"
OUT_DIR="${OUT_DIR:-/tmp/pi-debs}"
RASP_MIRROR="${RASP_MIRROR:-http://raspbian.raspberrypi.com/raspbian}"
RPI_MIRROR="${RPI_MIRROR:-https://archive.raspberrypi.com/debian}"
CACHE_DIR="${CACHE_DIR:-/tmp/apt-index-cache}"

SSH=(ssh -i "$SSH_KEY" -o ConnectTimeout=15 "$PI_HOST")

log() { echo "[offline_fetch] $*"; }
die() { log "ERROR: $*"; exit 1; }

mkdir -p "$OUT_DIR" "$CACHE_DIR"

# The archive keyrings come from the Pi itself (it has them even offline: they
# ship with the OS), or from KEYRING_DIR if set. They are what apt trusts there.
KEYRING_DIR="${KEYRING_DIR:-}"
keyring_for() {
    local name="$1" file
    case "$name" in
        raspbian) file="raspbian-archive-keyring.gpg" ;;
        rpi) file="raspberrypi-archive-keyring.gpg" ;;
        *) die "no keyring known for ${name}" ;;
    esac
    if [[ -n "$KEYRING_DIR" ]]; then
        [[ -f "${KEYRING_DIR}/${file}" ]] || die "keyring not found: ${KEYRING_DIR}/${file}"
        echo "${KEYRING_DIR}/${file}"
        return
    fi
    local dest="${CACHE_DIR}/${file}"
    if [[ ! -s "$dest" ]]; then
        "${SSH[@]}" "cat /usr/share/keyrings/${file}" >"$dest" 2>/dev/null || rm -f "$dest"
        [[ -s "$dest" ]] || die "could not read /usr/share/keyrings/${file} from the Pi (set KEYRING_DIR)"
    fi
    echo "$dest"
}

# The mirrors are plain HTTP (raspbian.raspberrypi.com has no HTTPS at all), and
# these debs get dpkg -i'd as root with no apt in between. So verify the way apt
# does: InRelease's signature against the archive keyring, Packages.gz against
# the SHA256 in that signed file, then every .deb against Packages (deb_ok).
fetch_index() {
    local name="$1" url="$2" dest="$3" tmp keyring want got
    [[ -f "$dest" ]] && return 0
    log "Fetching and verifying index ${name}..."
    tmp="$(mktemp -d)"
    keyring="$(keyring_for "$name")"
    curl -fsS -o "${tmp}/InRelease" "${url}/dists/${SUITE}/InRelease" || { rm -rf "$tmp"; die "${name}: no InRelease for ${SUITE}"; }
    gpgv --keyring "$keyring" --output "${tmp}/Release" "${tmp}/InRelease" 2>"${tmp}/gpgv.log" \
        || { cat "${tmp}/gpgv.log" >&2; rm -rf "$tmp"; die "${name}: InRelease signature did NOT verify"; }
    want="$(awk '/^SHA256:/{s=1; next} /^[^ ]/{s=0} s && $3 == "main/binary-armhf/Packages.gz" {print $1; exit}' "${tmp}/Release")"
    [[ -n "$want" ]] || { rm -rf "$tmp"; die "${name}: signed Release lists no main/binary-armhf/Packages.gz"; }
    curl -fsS -o "${tmp}/Packages.gz" "${url}/dists/${SUITE}/main/binary-armhf/Packages.gz" || { rm -rf "$tmp"; die "${name}: Packages.gz download failed"; }
    got="$(sha256sum "${tmp}/Packages.gz" | cut -d' ' -f1)"
    [[ "$got" == "$want" ]] || { rm -rf "$tmp"; die "${name}: Packages.gz does not match the signed Release (${got} != ${want})"; }
    gunzip -c "${tmp}/Packages.gz" >"${dest}.part" && mv -f "${dest}.part" "$dest"
    rm -rf "$tmp"
    log "  ${name}: signature OK, index matches the signed Release"
}

# One field of the package's stanza (Filename:, SHA256:, ...).
lookup_field() {
    local pkg="$1" field="$2" index="$3"
    awk -v p="$pkg" -v f="${field}:" '
        $1 == "Package:" && $2 == p { hit = 1; next }
        hit && $1 == f { print $2; exit }
        hit && $1 == "Package:" { exit }
    ' "$index"
}

lookup_filename() { lookup_field "$1" Filename "$2"; }

# Each .deb against the SHA256 in the verified index (fetch_index). Also catches
# a download cut short, which the "already have it" skip used to keep forever.
deb_ok() {
    local file="$1" want="$2"
    [[ -n "$want" && -f "$file" ]] || return 1
    [[ "$(sha256sum "$file" | cut -d' ' -f1)" == "$want" ]]
}

LIST="$(mktemp)"
trap 'rm -f "$LIST"' EXIT

log "Resolving packages to install for: ${PKG}"
"${SSH[@]}" "timeout 45 apt-get -s install -y ${PKG} 2>/dev/null | grep '^Inst' | awk '{print \$2}'" >"$LIST" || true
[[ -s "$LIST" ]] || die "empty package list — is Pi reachable?"

log "Packages to fetch: $(wc -l <"$LIST")"

# "-verified": caches written before signature checking must not be trusted.
RASP_INDEX="${CACHE_DIR}/raspbian-${SUITE}-armhf-Packages-verified"
RPI_INDEX="${CACHE_DIR}/rpi-${SUITE}-armhf-Packages-verified"
fetch_index raspbian "$RASP_MIRROR" "$RASP_INDEX"
fetch_index rpi "$RPI_MIRROR" "$RPI_INDEX"

failed=0
while read -r pkg; do
    fn="" sha="" index="$RASP_INDEX"
    fn="$(lookup_filename "$pkg" "$RASP_INDEX")"
    if [[ -z "$fn" ]]; then
        index="$RPI_INDEX"
        fn="$(lookup_filename "$pkg" "$RPI_INDEX")"
    fi
    if [[ -z "$fn" ]]; then
        log "skip (not in index): ${pkg}"
        continue
    fi
    sha="$(lookup_field "$pkg" SHA256 "$index")"
    out="${OUT_DIR}/$(basename "$fn")"
    if deb_ok "$out" "$sha"; then
        continue
    fi
    rm -f "$out"
    if curl -fsSLo "$out" "${RASP_MIRROR}/${fn}" 2>/dev/null && deb_ok "$out" "$sha"; then
        log "OK ${pkg}"
        continue
    fi
    if curl -fsSLo "$out" "${RPI_MIRROR}/${fn}" 2>/dev/null && deb_ok "$out" "$sha"; then
        log "OK ${pkg} (rpi mirror)"
        continue
    fi
    rm -f "$out"
    log "FAIL ${pkg} (${fn}) — download failed or SHA256 did not match the index"
    failed=$((failed + 1))
done <"$LIST"

log "Done. Debs in ${OUT_DIR} (failed: ${failed})"
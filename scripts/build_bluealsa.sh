#!/usr/bin/env bash
# Build bluez-alsa from source with all HD audio codecs.
#
# Why, per OS:
#   bookworm — bluez-alsa-utils 4.0.0 enables only SBC + LDAC for a2dp-source
#              and ships no bluealsa-cli, so AAC/aptX sinks are stuck on SBC and
#              the negotiated codec can never be switched.
#   trixie   — bluez-alsa-utils 4.3.1-3 does ship bluealsa-cli and adds
#              aptX/aptX-HD, but is still built without AAC. Only AAC is
#              missing, and only that is worth the build.
#
# This script builds:
#   - fdk-aac v2.0.3     (AAC encoder — no libfdk-aac-dev candidate exists on
#                         Raspbian armhf on either release, so it is built from
#                         source there; Debian proper ships it in non-free, and
#                         a packaged copy is detected and reused
#                         so that stage is skipped there; built from source on
#                         bookworm, which only has 2.0.2)
#   - bluez-alsa v4.3.1  (SBC/SBC-XQ, AAC, aptX, aptX-HD, LDAC + cli)
# installs to /usr/local, replaces the ALSA plugin, points the existing
# bluealsa.service at the new daemon, and enables BlueZ experimental D-Bus
# (required to expose remote SEPs for A2DP codec switching on BlueZ < 5.72).
#
# ~30-45 min on a Pi Zero 2W (bookworm), ~10-15 min on a 3 A+ (trixie: faster
# core, no fdk-aac stage). Idempotent: re-runs skip finished steps.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

FDK_VER="2.0.3"
BA_VER="4.3.1"
BUILD_ROOT="/var/tmp/bluealsa-build"
PREFIX="/usr/local"
# Multiarch triplet, not a hardcoded armhf path: keeps the script correct if the
# repo is ever installed on an arm64 image. dpkg-architecture comes with
# dpkg-dev, which install_build_deps only pulls in later, so fall back to the
# ALSA plugin dir that actually exists before settling on the armhf default.
_multiarch() {
    local out d
    out="$(dpkg-architecture -qDEB_HOST_MULTIARCH 2>/dev/null || true)"
    if [[ -z "$out" ]]; then
        for d in /usr/lib/*/alsa-lib; do
            [[ -d "$d" ]] || continue
            out="$(basename "$(dirname "$d")")"
            break
        done
    fi
    echo "${out:-arm-linux-gnueabihf}"
}
ALSA_PLUGIN_DIR="/usr/lib/$(_multiarch)/alsa-lib"
MAKE_JOBS=2
FORCE=false

BLUEALSA_OPTS=(
    -p a2dp-source
    -p a2dp-sink
    --keep-alive=5
    # The default. A speaker measured too slow for SBC-XQ (IKEA KALLSUP) gets
    # bluealsa restarted on --sbc-quality=high before it connects, via a
    # runtime drop-in: see bt_prepare_bluealsa in _common.sh.
    --sbc-quality=xq
    --aac-bitrate=320000
    --aac-afterburner
    --ldac-quality=high
)

usage() {
    cat <<'EOF'
Usage: build_bluealsa.sh [--force] [--jobs N]

Builds fdk-aac + bluez-alsa with AAC/aptX/aptX-HD/LDAC/SBC-XQ and
bluealsa-cli, installs to /usr/local and restarts bluealsa.service.

  --force     Rebuild even if the right version is already installed
  --jobs N    Parallel make jobs (default 2 — safe for 512 MB RAM)
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) FORCE=true; shift ;;
        --jobs) MAKE_JOBS="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

export PKG_CONFIG_PATH="${PREFIX}/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"

install_build_deps() {
    log "Installing build dependencies (apt)..."
    local pkgs=(
        build-essential autoconf automake libtool pkg-config
        curl ca-certificates
        libasound2-dev libbluetooth-dev libdbus-1-dev libglib2.0-dev
        libsbc-dev libldacbt-enc-dev libldacbt-abr-dev libfreeaptx-dev
    )
    # A packaged fdk-aac saves the slowest stage of this build, so take it when
    # it exists — but probe, never assume. Debian ships libfdk-aac-dev in
    # non-free; Raspbian trixie armhf does **not** (verified on a 3 A+ with
    # non-free enabled: no candidate at all), and adding the name unconditionally
    # made apt reject the entire build-dep list. build_fdk_aac() compiles 2.0.3
    # from source whenever pkg-config can't find one.
    if apt_pkg_available libfdk-aac-dev; then
        pkgs+=(libfdk-aac-dev)
    else
        log "libfdk-aac-dev not in apt here — fdk-aac ${FDK_VER} will be built from source"
    fi
    apt-get install -y --no-install-recommends "${pkgs[@]}"
}

fetch_and_unpack() {
    local url="$1" dir="$2"
    if [[ -d "$dir" ]]; then
        log "Source already unpacked: ${dir}"
        return 0
    fi
    log "Downloading ${url}"
    mkdir -p "$BUILD_ROOT"
    # Unpack into a scratch dir and move it into place only once complete. A
    # download cut short used to leave a half tree at $dir, which every later
    # run took for "already unpacked" — and failed to build, forever.
    local scratch
    scratch="$(mktemp -d "${BUILD_ROOT}/.unpack.XXXXXX")"
    if ! curl -fsSL "$url" | tar -xz -C "$scratch"; then
        rm -rf "${scratch:?}"
        die "download/unpack failed for ${url}"
    fi
    mv "${scratch}/$(basename "$dir")" "$dir" || { rm -rf "${scratch:?}"; die "unpack failed for ${url}"; }
    rm -rf "${scratch:?}"
    [[ -d "$dir" ]] || die "unpack failed for ${url}"
}

fdk_aac_installed() {
    pkg-config --exists "fdk-aac >= 2.0" 2>/dev/null
}

# True only when fdk-aac came from a distro package, not from our own
# /usr/local source build. Debian proper ships libfdk-aac-dev in non-free but
# Raspbian armhf does not — on either release — so this must be probed, never
# inferred from the OS codename.
fdk_aac_packaged() {
    local dir pc
    dir="$(pkg-config --variable=pcfiledir fdk-aac 2>/dev/null)" || return 1
    pc="${dir}/fdk-aac.pc"
    [[ -f "$pc" ]] && dpkg -S "$pc" >/dev/null 2>&1
}

build_fdk_aac() {
    # --force is deliberately not honoured against a *packaged* lib: rebuilding
    # it into /usr/local would shadow the package for no gain. It must still
    # rebuild our own source build, which the old release-based test blocked on
    # trixie — where apt has no candidate at all, so the lib is always ours.
    if fdk_aac_installed && { ! $FORCE || fdk_aac_packaged; }; then
        log "fdk-aac already installed ($(pkg-config --modversion fdk-aac)) — skipping"
        return 0
    fi
    local src="${BUILD_ROOT}/fdk-aac-${FDK_VER}"
    fetch_and_unpack \
        "https://github.com/mstorsjo/fdk-aac/archive/refs/tags/v${FDK_VER}.tar.gz" \
        "$src"
    log "Building fdk-aac ${FDK_VER} (this is the slow part)..."
    (
        cd "$src"
        [[ -x configure ]] || autoreconf -fiv
        ./configure --prefix="$PREFIX" --disable-static
        make "-j${MAKE_JOBS}"
        make install
    )
    ldconfig
    fdk_aac_installed || die "fdk-aac build finished but pkg-config cannot find it"
    log "fdk-aac ${FDK_VER} installed"
}

bluealsa_installed() {
    [[ -x "${PREFIX}/bin/bluealsa" ]] || return 1
    "${PREFIX}/bin/bluealsa" --version 2>/dev/null | grep -q "$BA_VER" || return 1
    "${PREFIX}/bin/bluealsa" --help 2>&1 | grep -q "AAC" || return 1
    [[ -x "${PREFIX}/bin/bluealsa-cli" ]]
}

build_bluealsa() {
    if bluealsa_installed && ! $FORCE; then
        log "bluez-alsa ${BA_VER} with AAC already installed — skipping build"
        return 0
    fi
    local src="${BUILD_ROOT}/bluez-alsa-${BA_VER}"
    fetch_and_unpack \
        "https://github.com/arkq/bluez-alsa/archive/refs/tags/v${BA_VER}.tar.gz" \
        "$src"
    log "Building bluez-alsa ${BA_VER}..."
    (
        cd "$src"
        [[ -x configure ]] || autoreconf -fiv
        mkdir -p build && cd build
        ../configure \
            --prefix="$PREFIX" \
            --localstatedir=/var \
            --enable-aac \
            --enable-aptx --enable-aptx-hd --with-libfreeaptx \
            --enable-ldac \
            --enable-cli \
            --with-alsaplugindir="$ALSA_PLUGIN_DIR" \
            --with-alsaconfdir=/etc/alsa/conf.d
        make "-j${MAKE_JOBS}"
        make install
    )
    ldconfig
    bluealsa_installed || die "bluez-alsa build finished but verification failed"
    log "bluez-alsa ${BA_VER} installed to ${PREFIX}"
}

divert_alsa_plugins() {
    # Debian-correct override: divert the packaged ALSA plugin files so
    # future libasound2-plugin-bluez upgrades install to *.distrib and never
    # clobber our build — packages stay upgradable (no apt-mark hold).
    local f
    for f in "${ALSA_PLUGIN_DIR}/libasound_module_pcm_bluealsa.so" \
             "${ALSA_PLUGIN_DIR}/libasound_module_ctl_bluealsa.so"; do
        if ! dpkg-divert --list "$f" 2>/dev/null | grep -q .; then
            dpkg-divert --add --rename --divert "${f}.distrib" "$f"
            # --rename moved the file currently at $f aside; restore it so
            # the plugin keeps working until (re)`make install` replaces it.
            if [[ ! -e "$f" && -e "${f}.distrib" ]]; then
                cp -p "${f}.distrib" "$f"
            fi
        fi
    done
    # Migrate installs that used the old apt-mark hold approach
    apt-mark unhold bluez-alsa-utils libasound2-plugin-bluez >/dev/null 2>&1 || true
    log "ALSA bluealsa plugins diverted (dpkg-divert) — packages left upgradable"
}

enable_bluez_experimental() {
    # BlueZ 5.66 (bookworm) exposes remote Stream End-Points (needed by
    # SelectCodec) only with experimental D-Bus interfaces enabled. Kept on for
    # 5.82 (trixie) too — same flag, verified path; if codec switching later
    # proves to work without it there, this is the line to drop.
    local conf="/etc/bluetooth/main.conf"
    if grep -qE '^\s*Experimental\s*=\s*true' "$conf" 2>/dev/null; then
        log "BlueZ Experimental already enabled"
        return 0
    fi
    if grep -qE '^\s*#?\s*Experimental\s*=' "$conf" 2>/dev/null; then
        sed -i -E 's|^\s*#?\s*Experimental\s*=.*|Experimental = true|' "$conf"
    else
        printf '\n[General]\nExperimental = true\n' >> "$conf"
    fi
    log "Enabled BlueZ Experimental (remote SEP D-Bus interface)"
    systemctl restart bluetooth.service
    sleep 3
}

install_service_override() {
    local dropin_dir="/etc/systemd/system/bluealsa.service.d"
    mkdir -p "$dropin_dir"
    cat > "${dropin_dir}/10-hd-codecs.conf" <<EOF
# Managed by build_bluealsa.sh — use the /usr/local build with HD codecs.
[Service]
# Wait for the BT adapter: starting while hci0 is still coming up makes
# bluealsa register its media application twice (AlreadyExists) and A2DP
# endpoints end up half-broken.
ExecStartPre=/bin/sh -c 'i=0; while [ ! -d /sys/class/bluetooth/hci0 ] && [ \$i -lt 60 ]; do sleep 0.5; i=\$((i+1)); done; sleep 2'
ExecStart=
ExecStart=${PREFIX}/bin/bluealsa ${BLUEALSA_OPTS[*]}
StateDirectory=bluealsa
EOF
    if [[ -f /lib/systemd/system/bluealsa-aplay.service ]]; then
        mkdir -p /etc/systemd/system/bluealsa-aplay.service.d
        cat > /etc/systemd/system/bluealsa-aplay.service.d/10-local-build.conf <<EOF
[Service]
ExecStart=
ExecStart=${PREFIX}/bin/bluealsa-aplay
EOF
    fi
    systemctl daemon-reload
    systemctl restart bluealsa.service
    systemctl restart bluealsa-aplay.service 2>/dev/null || true
    log "bluealsa.service now runs: ${PREFIX}/bin/bluealsa ${BLUEALSA_OPTS[*]}"
}

verify() {
    sleep 2
    systemctl is-active --quiet bluealsa.service || die "bluealsa.service not active after restart"
    # trixie's bluez-alsa-utils also ships /usr/bin/bluealsa-cli, so the scripts
    # that call it bare (bt_codec.sh, _common.sh) must resolve to our build —
    # it is the one talking to the daemon that is actually running.
    local cli
    cli="$(command -v bluealsa-cli || true)"
    if [[ "$cli" != "${PREFIX}/bin/bluealsa-cli" ]]; then
        warn "bluealsa-cli on PATH is '${cli:-none}', not ${PREFIX}/bin/bluealsa-cli — check that /usr/local/bin precedes /usr/bin"
    fi
    log "Enabled source codecs:"
    "${PREFIX}/bin/bluealsa" --help 2>&1 | sed -n '/Available BT audio codecs/,+3p' >&2
}

main() {
    [[ "${EUID}" -eq 0 ]] || die "run as root: sudo $0"
    require_cmd apt-get systemctl
    install_build_deps
    build_fdk_aac
    divert_alsa_plugins
    build_bluealsa
    enable_bluez_experimental
    install_service_override
    verify
    log "Done. Reconnect the BT device, then run bt_codec_select.sh to pick the best codec."
}

main "$@"

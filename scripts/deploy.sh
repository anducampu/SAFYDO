#!/usr/bin/env bash
# Deploy from dev machine to Pi — preserves live bluetooth.primary_mac
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PI_HOST="${PI_HOST:-safydo.local}"
PI_USER="${PI_USER:-${USER:-pi}}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
REMOTE_STAGE="${REMOTE_STAGE:-rpi-audio-player}"
INSTALL_DIR="${INSTALL_DIR:-/opt/rpi-audio-player}"

usage() {
    cat <<EOF
Usage: deploy.sh [--build] [--skip-install]

  --build         Run build_frontend.sh before rsync
  --skip-install  Rsync only; skip install.sh on Pi

Environment:
  PI_HOST   (default: safydo.local)
  PI_USER   (default: $USER, else pi)
  SSH_KEY   (default: ~/.ssh/id_ed25519)

WiFi: deploy always runs install with SKIP_WIFI_TUNING=1 (reboot-safe).
      Set APPLY_WIFI_TUNING=1 on the Pi install step to opt in to powersave drop-in.
EOF
}

ssh_pi() {
    ssh -i "${SSH_KEY}" -o ConnectTimeout=10 "${PI_USER}@${PI_HOST}" "$@"
}

remote_stage_path() {
    echo "/home/${PI_USER}/${REMOTE_STAGE}"
}

# NOTE: config/player.yaml is NOT excluded here — the stage copy is the
# merge template, so new template keys must reach the Pi. The live config
# is protected by the stage→install exclude + merge_player_config below.
# /public is the generated mirror (a second full copy of the tree) and /.private
# holds unredacted originals: neither belongs on a 512 MB board, and both used to
# ride along into /opt on every deploy. --delete-excluded prunes copies an older
# deploy left in the stage, which the stage->/opt --delete then removes too.
rsync_pi() {
    rsync -avz --delete --delete-excluded \
        --exclude .git \
        --exclude frontend/node_modules \
        --exclude /public/ \
        --exclude /.private/ \
        -e "ssh -i ${SSH_KEY}" \
        "${REPO}/" "${PI_USER}@${PI_HOST}:$(remote_stage_path)/"
}

do_build=false
do_install=true

while [[ $# -gt 0 ]]; do
    case "$1" in
        --build) do_build=true; shift ;;
        --skip-install) do_install=false; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage; exit 1 ;;
    esac
done

cd "${REPO}"
$do_build && ./scripts/build_frontend.sh

STAGE_PATH="$(remote_stage_path)"
echo "==> Rsync to ${PI_USER}@${PI_HOST}:${STAGE_PATH} (player.yaml is the merge template; the live one is merged, not overwritten)"
rsync_pi

echo "==> Merge config + install on Pi"
ssh_pi bash -s <<REMOTE
set -euo pipefail
INSTALL_DIR="${INSTALL_DIR}"
STAGE_PATH="${STAGE_PATH}"
LIVE="\${INSTALL_DIR}/config/player.yaml"
TEMPLATE="\${STAGE_PATH}/config/player.yaml"
BACKUP=""

sudo mkdir -p "\${INSTALL_DIR}/config"
if [[ -f "\$LIVE" ]]; then
    BACKUP="\$(mktemp)"
    sudo cp "\$LIVE" "\$BACKUP"
fi

if [[ "\$(find "\${STAGE_PATH}" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l)" -lt 3 ]]; then
    echo "ERROR: stage looks empty — aborting deploy" >&2
    exit 1
fi

# --delete so files removed from the repo are pruned on the Pi too (stale
# scripts/backend modules, obsolete config fragments). player.yaml is the live
# config (protected). frontend/dist is a gitignored build artifact handled
# separately below, so exclude it here — otherwise a no-build deploy (stage has
# no dist) would delete the deployed UI.
#
# streams.json and eq_presets.json are edited from the web UI, so they belong to
# the user once the board has them: the repo's copy only seeds a fresh board.
# Deploying over them silently threw away every station added in the UI.
sudo rsync -a --delete \
    --exclude '/config/player.yaml' \
    --exclude '/config/streams.json' \
    --exclude '/config/eq_presets.json' \
    --exclude '/frontend/dist' \
    "\${STAGE_PATH}/" "\${INSTALL_DIR}/"
for seed in streams.json eq_presets.json; do
    [[ -f "\${INSTALL_DIR}/config/\${seed}" ]] || sudo cp "\${STAGE_PATH}/config/\${seed}" "\${INSTALL_DIR}/config/\${seed}"
done

# Mirror the built UI only when the stage actually contains one — never wipe a
# working /opt UI on a no-build or fresh-clone deploy. --delete here prunes old
# hashed bundles so index-*.js/css don't pile up on the SD card.
if [[ -n "\$(ls -A "\${STAGE_PATH}/frontend/dist" 2>/dev/null)" ]]; then
    sudo rsync -a --delete "\${STAGE_PATH}/frontend/dist/" "\${INSTALL_DIR}/frontend/dist/"
    echo "Synced frontend/dist (pruned stale bundles)"
else
    echo "No frontend build in stage — left /opt UI untouched"
fi

if [[ -n "\$BACKUP" && -f "\$BACKUP" ]]; then
    sudo python3 "\${INSTALL_DIR}/scripts/merge_player_config.py" \
        --template "\$TEMPLATE" \
        --live "\$BACKUP" \
        --out "\$LIVE"
    rm -f "\$BACKUP"
    echo "Merged player.yaml (preserved primary_mac)"
else
    sudo cp "\$TEMPLATE" "\$LIVE"
    echo "Installed fresh player.yaml"
fi
REMOTE

if $do_install; then
    # Reboot-safe: never re-apply wifi-powersave-off.conf (breaks WiFi on Zero 2W reboot).
    # SAFYDO_HOSTNAME is forwarded when set; when unset install.sh keeps the
    # name the board already has (only a stock "raspberrypi" becomes "safydo"),
    # so deploying to a second board never renames it.
    ssh_pi "sudo SKIP_WIFI_TUNING=1 ${SAFYDO_HOSTNAME:+SAFYDO_HOSTNAME=${SAFYDO_HOSTNAME} }${INSTALL_DIR}/install.sh --skip-apt"
fi

echo "==> Done — http://${PI_HOST}/"
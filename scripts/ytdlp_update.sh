#!/usr/bin/env bash
# Weekly yt-dlp upgrade (audio-player-ytdlp-update.timer). YouTube changes
# break yt-dlp every few weeks and the fix ships as a release.
#   venv (/opt/yt-dlp, from install.sh): pip upgrade, keeping curl_cffi
#   zipapp fallback:                     yt-dlp -U
set -euo pipefail
VENV=/opt/yt-dlp
if [[ -x "${VENV}/bin/pip" ]]; then
    exec "${VENV}/bin/pip" install -q --disable-pip-version-check --upgrade 'yt-dlp[default,curl-cffi]'
fi
exec /usr/local/bin/yt-dlp -U

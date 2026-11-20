#!/usr/bin/env bash
# Graceful shutdown invoked from Web UI
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "safe_shutdown.sh requires root"
    exit 1
fi

# With the chip out of reset, a power cut can damage it. Do this first.
[[ -x /usr/local/lib/ugreen-dab+/radio_cli ]] && /usr/local/lib/ugreen-dab+/radio_cli -k || true
sync
echo "Shutting down in 3 seconds..."
sleep 3
/sbin/shutdown -h now
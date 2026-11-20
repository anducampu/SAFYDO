#!/usr/bin/env bash
# Prefer USB/Ethernet over WiFi when carrier is up (Phase 5)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

STATE_FILE="${STATE_DIR}/network_priority.json"
WLAN_IF="${WLAN_IF:-wlan0}"
ETH_METRIC="${ETH_METRIC:-100}"
WLAN_METRIC_STANDBY="${WLAN_METRIC_STANDBY:-200}"
WLAN_METRIC_ONLY="${WLAN_METRIC_ONLY:-600}"

enabled() {
    local flag
    flag="$(cfg_get network.ethernet_priority 2>/dev/null || echo true)"
    [[ "${flag,,}" != "false" && "${flag}" != "0" ]]
}

eth_has_ipv4() {
    ip -4 addr show "$1" 2>/dev/null | grep -qE 'inet [0-9]'
}

find_active_eth() {
    local eth iface
    for eth in /sys/class/net/eth* /sys/class/net/enx*; do
        [[ -d "$eth" ]] || continue
        iface="$(basename "$eth")"
        # Carrier alone is not enough — dongles can lie; require a real IPv4 lease.
        if [[ -f "${eth}/carrier" && "$(cat "${eth}/carrier" 2>/dev/null || echo 0)" == "1" ]] \
            && eth_has_ipv4 "$iface"; then
            echo "$iface"
            return 0
        fi
    done
    return 1
}

iface_up() {
    ip link show "$1" 2>/dev/null | grep -q "state UP"
}

# The gateway of THIS interface's default route. It used to be the first default
# route's gateway, installed on both interfaces — wrong (and refused by the
# kernel) whenever ethernet and WiFi sit on different subnets.
default_gw() {
    default_gateways | awk -v d="$1" '$2 == d {print $1; exit}'
}

# Runs every 30 s from the timer, so only write when something changed: the
# file lives in STATE_DIR, on the SD card.
write_state() {
    local mode="$1" eth="${2:-}" wlan_metric="${3:-}"
    mkdir -p "$STATE_DIR"
    python3 - "$STATE_FILE" "$mode" "$eth" "$WLAN_IF" "$wlan_metric" <<'PY'
import json, os, sys
from datetime import datetime, timezone

path, mode, eth, wlan, metric = sys.argv[1:6]
state = {
    "mode": mode,
    "eth_iface": eth or None,
    "wlan_iface": wlan,
    "wlan_metric": int(metric or 0) or None,
}
try:
    with open(path) as fh:
        old = json.load(fh)
    if {k: old.get(k) for k in state} == state:
        raise SystemExit(0)
except (OSError, ValueError, AttributeError):
    pass
state["updated_at"] = datetime.now(timezone.utc).isoformat()
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(state, fh, indent=2)
    fh.write("\n")
    fh.flush()
    os.fsync(fh.fileno())
os.replace(tmp, path)
PY
}

apply_ethernet_priority() {
    local eth="$1" gw wgw
    gw="$(default_gw "$eth")"
    [[ -n "$gw" ]] || { warn "no default gateway on ${eth} — skip metric tweak"; return 0; }

    ip route replace default via "$gw" dev "$eth" metric "$ETH_METRIC" 2>/dev/null || true
    wgw="$(default_gw "$WLAN_IF")"
    if iface_up "$WLAN_IF" && [[ -n "$wgw" ]]; then
        ip route replace default via "$wgw" dev "$WLAN_IF" metric "$WLAN_METRIC_STANDBY" 2>/dev/null || true
        log "Ethernet ${eth} preferred (metric ${ETH_METRIC}); ${WLAN_IF} standby (${WLAN_METRIC_STANDBY})"
    else
        log "Ethernet ${eth} preferred (metric ${ETH_METRIC})"
    fi
    write_state "ethernet" "$eth" "$WLAN_METRIC_STANDBY"
}

apply_wifi_only() {
    local gw
    gw="$(default_gw "$WLAN_IF")"
    if [[ -n "$gw" ]] && iface_up "$WLAN_IF"; then
        ip route replace default via "$gw" dev "$WLAN_IF" metric "$WLAN_METRIC_ONLY" 2>/dev/null || true
        log "WiFi ${WLAN_IF} primary (metric ${WLAN_METRIC_ONLY})"
    fi
    # Drop stale ethernet default routes when cable unplugged
    local eth
    for eth in /sys/class/net/eth* /sys/class/net/enx*; do
        [[ -d "$eth" ]] || continue
        local iface="$(basename "$eth")"
        if [[ "$(cat "${eth}/carrier" 2>/dev/null || echo 0)" != "1" ]]; then
            ip route del default dev "$iface" 2>/dev/null || true
        fi
    done
    write_state "wifi" "" "$WLAN_METRIC_ONLY"
}

main() {
    require_cmd ip python3
    if ! enabled; then
        log "ethernet_priority disabled in player.yaml"
        exit 0
    fi

    local eth=""
    if eth="$(find_active_eth)"; then
        apply_ethernet_priority "$eth"
    else
        apply_wifi_only
    fi
}

main "$@"
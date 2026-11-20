#!/usr/bin/env bash
# WiFi captive portal — AP mode for adding WPA networks (Phase 5)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

CONF_DIR="${INSTALL_DIR}/config/captive"
HOSTAPD_SRC="${CONF_DIR}/hostapd.conf"
DNSMASQ_SRC="${CONF_DIR}/dnsmasq.conf"
PORTAL_HTML="${CONF_DIR}/portal.html"
RUN_CONF="/run/audio-player/captive"
SCAN_CACHE="${RUN_CONF}/scan.json"
WPA_CONF="/etc/wpa_supplicant/wpa_supplicant.conf"
# Detect, do not assume. The onboard radio is disabled on these boxes, so the
# wireless device is a USB dongle — which under predictable naming is usually
# wlx<mac>, not wlan0. Hardcoding wlan0 is why the portal silently targeted a
# device that did not exist whenever a dongle was in use.
AP_IF="${AP_IF:-$(first_wireless_iface)}"
AP_SSID="${AP_SSID:-Safydo}"
AP_IP="${AP_IP:-192.168.4.1}"

usage() {
    cat <<EOF
Usage: captive_portal.sh <start|stop|status|add-wifi> [options]

  start              Start AP + dnsmasq + nginx portal
  stop               Stop AP mode, restore client WiFi
  status             Show captive portal state
  scan               List nearby networks as JSON (cached while the AP is up)
  add-wifi SSID [PASS|--password-file=PATH]  Append WPA network (open if PASS empty)
  forget-wifi ID       Remove saved WiFi profile (NetworkManager con-name or SSID)

Environment:
  AP_SSID  (default: Safydo)
EOF
}

require_root() {
    [[ "${EUID}" -eq 0 ]] || die "run as root"
}

wpa_has_networks() {
    if nm_managed_wifi; then
        nmcli -t -f TYPE con show 2>/dev/null | grep -q '^802-11-wireless$'
        return $?
    fi
    [[ -f "$WPA_CONF" ]] && grep -qE '^[[:space:]]*ssid=' "$WPA_CONF"
}

nm_managed_wifi() {
    command -v nmcli >/dev/null 2>&1 && systemctl is-active NetworkManager >/dev/null 2>&1
}

nm_con_exists() {
    nmcli -t -f NAME con show "$1" 2>/dev/null | grep -qxF "$1"
}

# ifname "*" — never pin the profile to the adapter that happened to be fitted
# when it was created. Swapping the uplink dongle is routine here, and the
# adapter also gets renamed when one comes or goes (a dongle is wlan1 beside an
# onboard radio and wlan0 once that radio is disabled). A profile welded to
# wlan0 then matches nothing and the box silently joins no network at all.
append_wifi_nm() {
    local ssid="$1" psk="$2"
    local id="${ssid// /_}"
    require_cmd nmcli

    if nm_con_exists "$id"; then
        # key-mgmt too: on a profile first saved as an open network the
        # security setting does not exist, and a bare psk is refused — which
        # `|| true` turned into "Updated" with no password stored.
        nmcli con modify "$id" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$psk" 2>/dev/null || true
        nmcli con modify "$id" connection.autoconnect yes 2>/dev/null || true
        # Heal profiles written by an older version, which pinned them.
        nmcli con modify "$id" connection.interface-name "" 2>/dev/null || true
        log "Updated NetworkManager WiFi: ${ssid}"
    else
        nmcli con add type wifi ifname "*" con-name "$id" ssid "$ssid" \
            wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$psk" connection.autoconnect yes
        log "Added NetworkManager WiFi: ${ssid}"
    fi
}

append_wifi_wpa() {
    local ssid="$1" psk="$2"
    require_cmd wpa_passphrase

    mkdir -p "$(dirname "$WPA_CONF")"
    touch "$WPA_CONF"
    if ! grep -q 'ctrl_interface=' "$WPA_CONF" 2>/dev/null; then
        cat >>"$WPA_CONF" <<'HDR'
ctrl_interface=DIR=/var/run/wpa_supplicant
update_config=1
country=RO

HDR
    fi

    {
        echo ""
        echo "network={"
        wpa_passphrase "$ssid" "$psk"
        echo "}"
    } >>"$WPA_CONF"
    log "Added wpa_supplicant WiFi: ${ssid}"
}

append_wifi() {
    local ssid="$1" psk="${2:-}"
    [[ -n "$ssid" ]] || die "usage: add-wifi SSID [PASSWORD|--password-file=PATH]"
    if [[ "$psk" == --password-file=* ]]; then
        local pfile="${psk#--password-file=}"
        [[ -f "$pfile" ]] || die "password file not found: $pfile"
        psk="$(<"$pfile")"
    fi
    if nm_managed_wifi; then
        if [[ -n "$psk" ]]; then
            append_wifi_nm "$ssid" "$psk"
        else
            local id="${ssid// /_}"
            # Check first: NetworkManager accepts a second profile with the same
            # name, so "add, else modify" never reached the modify — every re-add
            # stacked another duplicate beside the old (maybe secured) one.
            if nm_con_exists "$id"; then
                nmcli con modify "$id" remove 802-11-wireless-security 2>/dev/null || true
                nmcli con modify "$id" connection.autoconnect yes 2>/dev/null || true
            else
                nmcli con add type wifi ifname "*" con-name "$id" ssid "$ssid" \
                    connection.autoconnect yes 2>/dev/null || true
            fi
            nmcli con modify "$id" connection.interface-name "" 2>/dev/null || true
            log "Added open NetworkManager WiFi: ${ssid}"
        fi
    else
        [[ -n "$psk" ]] || die "open networks require NetworkManager (Bookworm)"
        append_wifi_wpa "$ssid" "$psk"
    fi
}

forget_wifi_nm() {
    local id="$1"
    require_cmd nmcli
    if nm_con_exists "$id"; then
        nmcli con delete "$id"
        log "Removed NetworkManager WiFi: ${id}"
        return 0
    fi
    # By SSID: profiles are named with spaces turned into underscores, so map
    # them back (`${con//_/ }` — the old `${con//_ / }` never matched). Read
    # whole lines; a `for` over the output split names at their spaces.
    local con
    while IFS= read -r con; do
        [[ -n "$con" ]] || continue
        if [[ "$con" == "$id" || "${con//_/ }" == "$id" ]]; then
            nmcli con delete "$con"
            log "Removed NetworkManager WiFi: ${con}"
            return 0
        fi
    done < <(nmcli -t -f NAME,TYPE con show 2>/dev/null | awk -F: '$2=="802-11-wireless"{print $1}')
    return 1
}

forget_wifi_wpa() {
    local ssid="$1"
    [[ -f "$WPA_CONF" ]] || return 1
    python3 - "$ssid" "$WPA_CONF" <<'PY'
import re, sys
from pathlib import Path
ssid, path = sys.argv[1], Path(sys.argv[2])
text = path.read_text()
blocks = re.split(r"(?=\nnetwork=\{)", text)
keep = [blocks[0]] if blocks else []
removed = False
for block in blocks[1:]:
    # The whole ssid= line, anchored: unanchored, forgetting "Home" also took
    # "Home2", and any block whose bssid= happened to contain the text.
    if re.search(rf'^\s*ssid="?{re.escape(ssid)}"?\s*$', block, re.MULTILINE):
        removed = True
        continue
    keep.append(block)
if not removed:
    raise SystemExit(1)
path.write_text("".join(keep))
PY
    log "Removed wpa_supplicant WiFi: ${ssid}"
}

forget_wifi() {
    local id="$1"
    [[ -n "$id" ]] || die "usage: forget-wifi ID"
    if nm_managed_wifi; then
        forget_wifi_nm "$id" || die "unknown WiFi profile: ${id}"
    else
        forget_wifi_wpa "$id" || die "unknown WiFi profile: ${id}"
    fi
}

stop_ap_services() {
    systemctl stop audio-player-captive-hostapd.service 2>/dev/null || true
    systemctl stop audio-player-captive-dnsmasq.service 2>/dev/null || true
    ip addr flush dev "$AP_IF" 2>/dev/null || true
    ip link set "$AP_IF" down 2>/dev/null || true
    rm -rf "$RUN_CONF"
}

# The part of start that can fail once the radio has been taken from
# NetworkManager. Explicit returns: it runs as an `if` condition, where set -e
# does not apply.
activate_ap() {
    systemctl stop wpa_supplicant@"${AP_IF}".service 2>/dev/null || true
    systemctl stop dhcpcd 2>/dev/null || true
    rfkill unblock wifi 2>/dev/null || true
    ip link set "$AP_IF" up || return 1
    ip addr flush dev "$AP_IF" || return 1
    ip addr add "${AP_IP}/24" dev "$AP_IF" || return 1

    ln -sf "${RUN_CONF}/hostapd.conf" /etc/hostapd/hostapd.conf 2>/dev/null || true
    systemctl start audio-player-captive-hostapd.service || return 1
    systemctl start audio-player-captive-dnsmasq.service || return 1
}

start_portal() {
    require_root
    require_cmd hostapd dnsmasq nginx ip

    [[ -f "$HOSTAPD_SRC" && -f "$DNSMASQ_SRC" ]] || die "missing config in ${CONF_DIR}"

    mkdir -p "$RUN_CONF"
    cp "$HOSTAPD_SRC" "${RUN_CONF}/hostapd.conf"
    cp "$DNSMASQ_SRC" "${RUN_CONF}/dnsmasq.conf"
    sed -i "s/^ssid=.*/ssid=${AP_SSID}/" "${RUN_CONF}/hostapd.conf"

    if [[ -z "$AP_IF" ]]; then
        die "no wireless interface found — plug in a WiFi dongle (an AP needs a radio), or set AP_IF="
    fi
    if [[ ! -d "/sys/class/net/${AP_IF}/wireless" ]]; then
        die "${AP_IF} is not a wireless interface — an access point cannot run on it"
    fi

    # The dnsmasq package enables a system-wide resolver on :53. Ours serves
    # DHCP to the AP clients and cannot bind while that one holds the port, so
    # the SSID comes up and every client associates, gets no lease and drops —
    # which looks exactly like "the AP is broken".
    if systemctl is-active --quiet dnsmasq.service 2>/dev/null; then
        log "stopping the system dnsmasq — it holds :53 and would block the portal DHCP"
        systemctl stop dnsmasq.service 2>/dev/null || true
    fi

    sed -i "s/^interface=.*/interface=${AP_IF}/" "${RUN_CONF}/hostapd.conf"
    sed -i "s/^interface=.*/interface=${AP_IF}/" "${RUN_CONF}/dnsmasq.conf"
    sed -i "s/^dhcp-range=.*/dhcp-range=${AP_IP%.*}.10,${AP_IP%.*}.50,255.255.255.0,12h/" "${RUN_CONF}/dnsmasq.conf"

    # Last chance to see the neighbourhood: from here the radio is an AP.
    survey_networks "$AP_IF"

    # From the moment NetworkManager lets go of the radio, a failure must hand
    # it back: under set -e a hostapd that would not start used to end the
    # script right here, leaving the adapter unmanaged — no AP and no client
    # WiFi either, even after the home network came back.
    nmcli dev set "$AP_IF" managed no 2>/dev/null || true
    if ! activate_ap; then
        warn "access point failed to start on ${AP_IF} — handing it back to client mode"
        stop_ap_services
        restore_client_wifi
        die "captive portal could not start on ${AP_IF}"
    fi

    # nginx serves portal from config/captive via install.sh drop-in
    systemctl reload nginx 2>/dev/null || true
    echo "active" >"${RUN_CONF}/state"
    log "Captive portal active — SSID: ${AP_SSID} — http://${AP_IP}/"
}

restore_client_wifi() {
    ip addr flush dev "$AP_IF" 2>/dev/null || true
    ip link set "$AP_IF" up 2>/dev/null || true
    if nm_managed_wifi; then
        nmcli dev set "$AP_IF" managed yes 2>/dev/null || true
        nmcli radio wifi on 2>/dev/null || true
        "${SCRIPT_DIR}/network_ready.sh" 2>/dev/null || nmcli dev connect "$AP_IF" 2>/dev/null || true
        log "Captive portal stopped — NetworkManager client WiFi restored"
    else
        systemctl restart wpa_supplicant@"${AP_IF}".service 2>/dev/null || true
        systemctl restart dhcpcd 2>/dev/null || true
        log "Captive portal stopped — wpa_supplicant client WiFi restored"
    fi
}

stop_portal() {
    require_root
    stop_ap_services
    restore_client_wifi
}

cmd_status() {
    if [[ -f "${RUN_CONF}/state" ]]; then
        echo "captive_portal: active"
        echo "ssid: ${AP_SSID}"
        echo "ip: ${AP_IP}"
    else
        echo "captive_portal: inactive"
    fi
    if wpa_has_networks; then
        echo "saved_networks: yes"
    else
        echo "saved_networks: no"
    fi
}

# The portal used to offer nothing but a blank SSID box, which means typing a
# network name exactly right from memory. Offer the neighbourhood instead.
#
# The catch on a one-radio box: once hostapd owns the interface the driver
# cannot scan, so the survey has to be taken while the radio is still in client
# mode, just before the AP goes up, and cached for the page to read.
networks_to_json() {
    python3 - "$1" "$2" <<'PY'
import json, re, sys, time

mode = sys.argv[1]
raw = sys.argv[2]
nets = {}


def add(ssid, percent, secure):
    ssid = (ssid or "").strip()
    if not ssid:                     # hidden network — nothing to offer
        return
    percent = max(0, min(100, int(percent)))
    prev = nets.get(ssid)
    if prev is None or percent > prev["signal"]:
        nets[ssid] = {"ssid": ssid, "signal": percent, "secure": bool(secure)}


if mode == "nmcli":
    # nmcli -t escapes ':' and '\\' inside values, so split on unescaped colons.
    for line in raw.splitlines():
        parts, cur, i = [], "", 0
        while i < len(line):
            c = line[i]
            if c == "\\" and i + 1 < len(line):
                cur += line[i + 1]
                i += 2
            elif c == ":":
                parts.append(cur)
                cur = ""
                i += 1
            else:
                cur += c
                i += 1
        parts.append(cur)
        if len(parts) < 3:
            continue
        signal, security, ssid = parts[0], parts[1], ":".join(parts[2:])
        try:
            signal = int(signal or 0)
        except ValueError:
            signal = 0
        secure = bool(security.strip()) and security.strip() != "--"
        add(ssid, signal, secure)
else:
    cur = {}
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("BSS "):
            if cur:
                add(cur.get("ssid"), cur.get("percent", 0), cur.get("secure"))
            cur = {"secure": False, "percent": 0}
        elif s.startswith("signal:"):
            m = re.search(r"(-?\d+(?:\.\d+)?)", s)
            if m:                    # dBm -> rough percent, same curve nmcli uses
                cur["percent"] = 2 * (float(m.group(1)) + 100)
        elif s.startswith("SSID:"):
            cur["ssid"] = s[5:].strip()
        elif s.startswith(("RSN:", "WPA:")) or "Privacy" in s:
            cur["secure"] = True
    if cur:
        add(cur.get("ssid"), cur.get("percent", 0), cur.get("secure"))

out = sorted(nets.values(), key=lambda n: n["signal"], reverse=True)
json.dump({"networks": out, "scanned_at": int(time.time())}, sys.stdout)
PY
}

survey_networks() {
    local iface="${1:-$AP_IF}"
    [[ -n "$iface" ]] || return 0
    mkdir -p "$RUN_CONF"
    # The radio may be soft-blocked (see network_ready.sh) — a blocked radio
    # scans nothing and reports no error worth reading.
    rfkill unblock wifi 2>/dev/null || true
    ip link set "$iface" up 2>/dev/null || true

    local raw="" mode="nmcli"
    if nm_managed_wifi; then
        # SSID last: it is the only field that can contain an escaped colon.
        raw="$(nmcli -t -f SIGNAL,SECURITY,SSID dev wifi list ifname "$iface" --rescan yes 2>/dev/null || true)"
    fi
    if [[ -z "$raw" ]]; then
        mode="iw"
        raw="$(iw dev "$iface" scan 2>/dev/null || true)"
    fi
    [[ -n "$raw" ]] || { log "no networks found while surveying on ${iface}"; return 0; }

    if networks_to_json "$mode" "$raw" >"${SCAN_CACHE}.tmp" 2>/dev/null; then
        mv "${SCAN_CACHE}.tmp" "$SCAN_CACHE"
        log "surveyed $(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["networks"]))' "$SCAN_CACHE" 2>/dev/null || echo "?") nearby networks"
    else
        rm -f "${SCAN_CACHE}.tmp"
    fi
}

cmd_scan() {
    # While the AP is up the radio cannot scan, so serve the survey taken just
    # before it went up. With no AP running, take a fresh one.
    if [[ ! -f "${RUN_CONF}/state" ]]; then
        require_root
        survey_networks "$AP_IF"
    fi
    if [[ -f "$SCAN_CACHE" ]]; then
        cat "$SCAN_CACHE"
    else
        echo '{"networks": [], "scanned_at": 0}'
    fi
}

main() {
    local cmd="${1:-}"
    case "$cmd" in
        start) start_portal ;;
        stop) stop_portal ;;
        status) cmd_status ;;
        add-wifi) append_wifi "${2:-}" "${3:-}" ;;
        forget-wifi) forget_wifi "${2:-}" ;;
        scan) cmd_scan ;;
        -h|--help|help) usage ;;
        *) usage; exit 1 ;;
    esac
}

main "$@"
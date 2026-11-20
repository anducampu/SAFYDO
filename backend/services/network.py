"""Network helpers — priority state, captive portal, WiFi."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from backend.config import INSTALL_DIR, cfg_get
from backend.services.scripts import run_script

STATE_DIR = Path("/var/lib/audio-player")
NETWORK_STATE = STATE_DIR / "network_priority.json"
CAPTIVE_STATE = Path("/run/audio-player/captive/state")


def _run(cmd: list[str], timeout: int = 10) -> str:
    if not shutil.which(cmd[0]):
        return ""
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout, check=False)
    return (proc.stdout or "").strip()


def _nm_managed() -> bool:
    if not shutil.which("nmcli"):
        return False
    proc = subprocess.run(
        ["systemctl", "is-active", "NetworkManager"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=5,
        check=False,
    )
    return proc.stdout.strip() == "active"


def _wifi_iface() -> str | None:
    """First wireless interface — wlan0 (onboard) or wlx* (USB dongle)."""
    try:
        for p in sorted(Path("/sys/class/net").iterdir()):
            if (p / "wireless").is_dir():
                return p.name
    except OSError:
        pass
    return None


def _default_link() -> dict:
    """Interface carrying the default route (the actual internet path)."""
    iface = gw = None
    for line in _run(["ip", "-4", "route", "show", "default"]).splitlines():
        parts = line.split()
        if "via" in parts:
            gw = parts[parts.index("via") + 1]
        if "dev" in parts:
            iface = parts[parts.index("dev") + 1]
        break
    ltype = None
    if iface:
        if Path(f"/sys/class/net/{iface}/wireless").is_dir():
            ltype = "wifi"
        elif iface.startswith("usb"):
            ltype = "usb-gadget"
        else:
            ltype = "ethernet"
    ip4 = None
    if iface:
        for line in _run(["ip", "-4", "-o", "addr", "show", iface]).splitlines():
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
            if m:
                ip4 = m.group(1)
                break
    return {"iface": iface, "type": ltype, "gateway": gw, "ip4": ip4}


def _iface_dns(iface: str | None) -> list[str]:
    """DNS servers for the active connection on iface (NM)."""
    if not iface or not _nm_managed():
        return []
    out: list[str] = []
    for line in _run(["nmcli", "-t", "-f", "IP4.DNS", "dev", "show", iface]).splitlines():
        if line.startswith("IP4.DNS"):
            val = line.split(":", 1)[1].strip()
            if val:
                out.append(val)
    return out


def _ip_method(iface: str | None) -> str | None:
    """'dhcp' or 'static' for the active connection on iface (NM), else None."""
    if not iface or not _nm_managed():
        return None
    con = None
    for line in _run(["nmcli", "-t", "-f", "GENERAL.CONNECTION", "dev", "show", iface]).splitlines():
        if line.startswith("GENERAL.CONNECTION:"):
            con = line.split(":", 1)[1] or None
    if not con:
        return None
    for line in _run(["nmcli", "-t", "-f", "ipv4.method", "con", "show", con]).splitlines():
        if line.startswith("ipv4.method:"):
            method = line.split(":", 1)[1]
            return {"auto": "dhcp", "manual": "static"}.get(method, method or None)
    return None


def _read_nm_wifi(wif: str | None = None) -> dict:
    info: dict = {"ssid": None, "ip4": None, "gateway": None, "dns": [], "saved_networks": []}
    if not _nm_managed():
        return info

    for line in _run(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"]).splitlines():
        if line.startswith("yes:"):
            info["ssid"] = line.split(":", 1)[1] or None
            break

    wif = wif or _wifi_iface() or "wlan0"
    dev = _run(["nmcli", "-t", "-f", "GENERAL.CONNECTION,GENERAL.STATE", "dev", "show", wif])
    for line in dev.splitlines():
        if line.startswith("GENERAL.CONNECTION:"):
            info["connection"] = line.split(":", 1)[1] or None

    for line in _run(
        ["nmcli", "-t", "-f", "IP4.ADDRESS,IP4.GATEWAY,IP4.DNS", "dev", "show", wif]
    ).splitlines():
        if line.startswith("IP4.ADDRESS"):
            info["ip4"] = line.split(":", 1)[1].split("/")[0] or None
        elif line.startswith("IP4.GATEWAY"):
            info["gateway"] = line.split(":", 1)[1] or None
        elif line.startswith("IP4.DNS"):
            dns = line.split(":", 1)[1]
            if dns:
                info["dns"].append(dns)

    saved: list[dict] = []
    for line in _run(["nmcli", "-t", "-f", "NAME,TYPE", "con", "show"]).splitlines():
        parts = line.split(":")
        if len(parts) < 2 or parts[1] != "802-11-wireless":
            continue
        name = parts[0]
        if name in ("preconfigured", "Wired connection 1"):
            continue
        saved.append({"id": name, "ssid": name.replace("_", " ")})
    info["saved_networks"] = saved
    return info


def _read_ip_fallback(wif: str | None = None) -> dict:
    info: dict = {"ssid": None, "ip4": None, "gateway": None, "dns": [], "saved_networks": []}
    wif = wif or _wifi_iface() or "wlan0"
    for line in _run(["ip", "-4", "-o", "addr", "show", wif]).splitlines():
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
        if m:
            info["ip4"] = m.group(1)
            break
    for line in _run(["ip", "-4", "route", "show", "default"]).splitlines():
        parts = line.split()
        # Only report a real gateway (default via <ip>); a link-scope route
        # (default dev X scope link) has no gateway — parts[2] would be the
        # interface name, not an address.
        if "via" in parts:
            info["gateway"] = parts[parts.index("via") + 1]
            break
    wpa = Path("/etc/wpa_supplicant/wpa_supplicant.conf")
    if wpa.is_file():
        ssids = re.findall(r'^\s*ssid="?([^"\n]+)"?', wpa.read_text(), re.MULTILINE)
        info["saved_networks"] = [{"id": s, "ssid": s} for s in ssids]
    return info


def _read_wifi_signal(wif: str | None = None) -> dict:
    """WiFi link quality: percent (0-100) + dBm when available."""
    percent: int | None = None
    dbm: int | None = None
    wif = wif or _wifi_iface() or "wlan0"
    # /proc/net/wireless is always readable: "wlan0: 0000   58.  -52.  -256 ..."
    try:
        for line in Path("/proc/net/wireless").read_text().splitlines():
            if f"{wif}:" not in line:
                continue
            fields = line.split()
            if len(fields) >= 4:
                quality = float(fields[2].rstrip("."))
                percent = max(0, min(100, round(quality / 70 * 100)))
                dbm = round(float(fields[3].rstrip(".")))
            break
    except (OSError, ValueError):
        pass
    if percent is None and _nm_managed():
        for line in _run(["nmcli", "-t", "-f", "ACTIVE,SIGNAL", "dev", "wifi"]).splitlines():
            if line.startswith("yes:"):
                try:
                    percent = max(0, min(100, int(line.split(":", 1)[1])))
                except ValueError:
                    pass
                break
    return {"wifi_signal": percent, "wifi_dbm": dbm}


def read_network_status() -> dict:
    status: dict = {
        "captive_portal": CAPTIVE_STATE.is_file(),
        "ethernet_priority": str(cfg_get("network.ethernet_priority") or "false").lower()
        in ("true", "1", "yes"),
        "hostname": cfg_get("hostname") or "safydo",
        "mdns": cfg_get("mdns_name") or "safydo.local",
    }
    if NETWORK_STATE.exists():
        try:
            status["priority"] = json.loads(NETWORK_STATE.read_text())
        except json.JSONDecodeError:
            status["priority"] = None
    else:
        status["priority"] = None

    wif = _wifi_iface()  # scan /sys/class/net once; reused by every helper below
    wifi = _read_nm_wifi(wif) if _nm_managed() else _read_ip_fallback(wif)
    if not wifi.get("ip4"):
        fb = _read_ip_fallback(wif)
        wifi.setdefault("ip4", fb.get("ip4"))
        wifi.setdefault("gateway", fb.get("gateway"))
        if not wifi.get("saved_networks"):
            wifi["saved_networks"] = fb.get("saved_networks", [])
    status.update(wifi)
    status.update(_read_wifi_signal(wif))

    # The internet path may be an ethernet dongle (no wifi fields at all).
    link = _default_link()
    status["link"] = link
    status["wifi_present"] = wif is not None
    status["ip_method"] = _ip_method(link["iface"])
    if link["ip4"] and link["type"] not in ("wifi", "usb-gadget", None):
        # A non-WiFi dongle carries the live default route — show its address,
        # DNS and gateway, not the stale WiFi values that update(wifi) left.
        status["ip4"] = link["ip4"]
        status["gateway"] = link["gateway"]
        link_dns = _iface_dns(link["iface"])
        if link_dns:
            status["dns"] = link_dns
    else:
        if not status.get("ip4") and link["ip4"]:
            status["ip4"] = link["ip4"]
        if not status.get("gateway") and link["gateway"]:
            status["gateway"] = link["gateway"]
    status["online"] = bool(link["iface"] or wifi.get("ip4"))
    return status




def add_wifi(ssid: str, password: str) -> dict:
    import os
    import tempfile

    secret = ""
    try:
        if password:
            with tempfile.NamedTemporaryFile("w", delete=False, prefix="wifi-") as fh:
                os.fchmod(fh.fileno(), 0o600)
                fh.write(password)
                secret = fh.name
            proc = run_script(
                "captive_portal.sh", "add-wifi", ssid, f"--password-file={secret}", timeout=30
            )
        else:
            proc = run_script("captive_portal.sh", "add-wifi", ssid, "", timeout=30)
    finally:
        if secret:
            Path(secret).unlink(missing_ok=True)
    if proc.returncode != 0:
        return {"ok": False, "stderr": proc.stderr.strip(), "stdout": proc.stdout.strip()}
    stop = run_script("captive_portal.sh", "stop", timeout=60)
    return {
        "ok": True,
        "saved": ssid,
        "captive_stopped": stop.returncode == 0,
        "message": "Network saved — reconnecting to WiFi",
    }


def captive_start() -> dict:
    proc = run_script("captive_portal.sh", "start", timeout=60)
    if proc.returncode != 0:
        return {"ok": False, "detail": proc.stderr.strip() or proc.stdout.strip()}
    enabled = Path("/etc/nginx/sites-enabled/audio-player-captive")
    src = INSTALL_DIR / "config/nginx/captive.conf"
    if src.is_file() and not enabled.exists():
        enabled.symlink_to(src)
        subprocess.run(["systemctl", "reload", "nginx"], check=False)
    return {"ok": True, "ssid": "Safydo", "ip": "192.168.4.1"}


def scan_wifi() -> dict:
    """Nearby networks for the setup portal's picker.

    A one-radio box cannot scan while hostapd owns the interface, so
    captive_portal.sh takes the survey just before the AP goes up and caches it;
    `scan` serves that cache while the AP is live and scans for real otherwise.
    Either way the portal offers a list instead of a blank SSID box.
    """
    try:
        proc = run_script("captive_portal.sh", "scan", timeout=45)
    except (OSError, subprocess.SubprocessError):
        return {"networks": [], "scanned_at": 0, "cached": False}
    if proc.returncode != 0:
        return {"networks": [], "scanned_at": 0, "cached": False}
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return {"networks": [], "scanned_at": 0, "cached": False}
    data.setdefault("networks", [])
    data.setdefault("scanned_at", 0)
    # Tell the page whether it is looking at a live sweep or the pre-AP survey,
    # so it can say so rather than imply the list is current.
    data["cached"] = CAPTIVE_STATE.exists()
    return data


def forget_wifi(network_id: str) -> dict:
    proc = run_script("captive_portal.sh", "forget-wifi", network_id, timeout=30)
    if proc.returncode != 0:
        return {"ok": False, "stderr": proc.stderr.strip(), "stdout": proc.stdout.strip()}
    return {"ok": True, "removed": network_id}


def captive_stop() -> dict:
    proc = run_script("captive_portal.sh", "stop", timeout=60)
    enabled = Path("/etc/nginx/sites-enabled/audio-player-captive")
    if enabled.exists() or enabled.is_symlink():
        enabled.unlink(missing_ok=True)
        subprocess.run(["systemctl", "reload", "nginx"], check=False)
    return {"ok": proc.returncode == 0, "stdout": proc.stdout.strip()}
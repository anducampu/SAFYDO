"""Bluetooth device discovery, pairing, and primary device management."""

from __future__ import annotations

import json
import re
import subprocess
import time
from functools import lru_cache
from pathlib import Path

from backend.services.scripts import run_script as _run_script
from backend.textsafe import display

INSTALL_DIR = Path("/opt/rpi-audio-player")
KNOWN_DEVICES_FILE = Path("/var/lib/audio-player/known_devices.json")
RUN_DIR = Path("/run/audio-player")
STATE_DIR = Path("/var/lib/audio-player")
# bt_watchdog.sh writes its verdict here rather than failing the unit. The
# volatile copy is the current one; the STATE_DIR copy holds the last verdict
# that needed a human and survives the reboot it may describe.
WATCHDOG_STATUS = RUN_DIR / "bt_watchdog.status"
WATCHDOG_STATUS_LAST = STATE_DIR / "bt_watchdog.status"
# Deadline epoch of an in-progress user scan; the bt-retry timer honours it
# (bt_autoconnect.sh retry_busy) so it never pages the radio mid-discovery.
SCAN_SENTINEL = RUN_DIR / "scan.active"

_MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$", re.IGNORECASE)

_DEVICE_LINE = re.compile(
    r"^Device\s+([0-9A-F]{2}(?::[0-9A-F]{2}){5})\s+(.+)$",
    re.IGNORECASE,
)
_SCAN_LINE = re.compile(
    r"^\s*([0-9A-F]{2}(?::[0-9A-F]{2}){5})\s+(.+?)(?:\s+(\[[^\]]+\]))?$",
    re.IGNORECASE,
)
# BlueZ uses the dashed MAC as Alias when the real name was never resolved
_PLACEHOLDER_NAME = re.compile(r"^[0-9A-F]{2}(?:-[0-9A-F]{2}){5}$", re.IGNORECASE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\r")
_PROMPT_RE = re.compile(r"^\[[^\]]*\]#\s*")
_INFO_HEADER = re.compile(
    r"^Device\s+([0-9A-F]{2}(?::[0-9A-F]{2}){5})", re.IGNORECASE
)
_AUDIO_ICONS = {"audio-card", "audio-headset", "audio-headphones", "audio-speakers"}


def _cfg_get(key: str):
    from backend.config import cfg_get

    return cfg_get(key)


def _cfg_set(key: str, value: str) -> None:
    from backend.config import cfg_set

    cfg_set(key, value)


@lru_cache(maxsize=1)
def rebind_viable() -> str:
    """Can unbinding the BT driver power-cycle this board's chip? yes|no|unknown.

    A property of the hardware, so it is read once per process. It decides what
    the UI can honestly offer: where the answer is not "yes", a reboot is the
    only recovery from a wedged controller and the user is the one who gets to
    decide whether the box may take it.
    """
    try:
        proc = subprocess.run(
            ["bash", "-c",
             f'source "{INSTALL_DIR}/scripts/_common.sh" >/dev/null 2>&1; bt_rebind_viable_str'],
            capture_output=True, text=True, errors="replace", timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    verdict = proc.stdout.strip()
    return verdict if verdict in ("yes", "no") else "unknown"


def _watchdog_line(path: Path) -> tuple[str, str]:
    try:
        raw = path.read_text().strip()
    except OSError:
        return "", ""
    at, _, verdict = raw.partition(" ")
    return (at, verdict) if verdict else ("", at)


_watchdog_cache: dict = {"at": 0.0, "value": None}
# The status frame this rides on is built at 1 Hz; the watchdog writes at most
# once a minute. Same treatment as link_signal for the same reason.
_WATCHDOG_INTERVAL = 5.0


def watchdog_info() -> dict:
    """What the wedge watchdog last concluded, for the UI and diag.

    Without this the one state a human has to act on — "STUCK: unrecoverable
    without a reboot" — was written to a tmpfs file nothing read, on a box whose
    journal is volatile.
    """
    now = time.monotonic()
    if _watchdog_cache["value"] is not None and now - _watchdog_cache["at"] < _WATCHDOG_INTERVAL:
        return _watchdog_cache["value"]
    at, verdict = _watchdog_line(WATCHDOG_STATUS)
    last_at, last = _watchdog_line(WATCHDOG_STATUS_LAST)
    value = {
        "rebind_viable": rebind_viable(),
        # Needed to say something true in the UI: with escalation off there is
        # no "it will retry shortly", there is only "someone has to reboot it".
        "reboot_enabled": str(_cfg_get("bluetooth.wedge_reboot") or "false").lower()
        in ("true", "1", "yes"),
        "verdict": verdict,
        "at": at,
        "last_event": last,
        "last_event_at": last_at,
        # wedged: the controller is known to be faulty right now, including
        # while recovery sits in its cooldown — a live box showed that gap,
        # reporting nothing at all for five minutes mid-fault.
        # stuck: and there is nothing left for it to try.
        "wedged": verdict.startswith(("STUCK", "waiting:", "rebooting:")),
        "stuck": verdict.startswith("STUCK"),
    }
    _watchdog_cache.update({"at": now, "value": value})
    return value


def _refresh_bt_status() -> dict:
    from backend.services.system import read_bt_status, refresh_bt_status

    return refresh_bt_status()


def _read_bt_status() -> dict:
    from backend.services.system import read_bt_status

    return read_bt_status()


def normalize_bt_status(raw: dict) -> dict:
    bitrate = raw.get("bitrate")
    if not bitrate and raw.get("bitrate_kbps"):
        bitrate = f"{raw['bitrate_kbps']} kbps"
    return {
        "connected": bool(raw.get("connected")),
        "mac": raw.get("device_mac") or raw.get("mac"),
        "device": raw.get("device_name") or raw.get("device"),
        "codec": raw.get("codec"),
        "bitrate": bitrate,
        "sample_rate": raw.get("sample_rate"),
        "device_codecs": raw.get("device_codecs") or [],
        "pi_codecs": raw.get("pi_codecs") or [],
        "best_codec": raw.get("best_codec"),
        "is_best_quality": bool(raw.get("is_best_quality")),
        # Speaker measured unable to sustain SBC-XQ (xq_probe); it runs standard SBC.
        "xq_denied": bool(raw.get("xq_denied")),
        "audio_device": raw.get("audio_device"),
        "updated_at": raw.get("updated_at"),
    }


# hcitool is cheap (~5 ms) but it is still two processes, and read_bt_status()
# runs on every 1 Hz status frame. Sample at this interval and serve the cache in
# between.
_SIGNAL_INTERVAL = 5.0
_signal_cache: dict = {"mac": None, "at": 0.0, "value": None}
_LQ_RE = re.compile(r"Link quality:\s*(\d+)")
_RSSI_RE = re.compile(r"RSSI return value:\s*(-?\d+)")


def _hcitool(sub: str, mac: str) -> str:
    """Run one hcitool query. Empty string on any failure.

    Safe on a device that is not connected: hcitool looks the ACL handle up and
    prints "Not connected." — it never tries to create a link, so this cannot
    page the radio behind the user's back.
    """
    try:
        proc = subprocess.run(
            ["hcitool", sub, mac],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout or ""


def link_signal(mac: str | None) -> dict | None:
    """Radio signal of the live ACL link, or None when there is nothing to ask.

    Two numbers, and they are not the same kind of thing:

      quality — HCI_Read_Link_Quality, 0-255. Vendor-defined but monotonic, and
                on BR/EDR it tracks the retransmission rate. Reported as a
                percentage, which is the honest reading of it.
      rssi    — HCI_Read_RSSI. On BR/EDR this is **not** dBm: it is decibels
                above or below the receiver's golden receive range, so 0 means
                "comfortably in range" and small negatives are normal. Only the
                RSSI in an inquiry/advertising report is absolute dBm. Never
                label this one dBm in the UI.
    """
    if not mac:
        return None
    now = time.monotonic()
    if _signal_cache["mac"] == mac and now - _signal_cache["at"] < _SIGNAL_INTERVAL:
        return _signal_cache["value"]
    m_lq = _LQ_RE.search(_hcitool("lq", mac))
    m_rssi = _RSSI_RE.search(_hcitool("rssi", mac))
    value = None
    if m_lq or m_rssi:
        quality = int(m_lq.group(1)) if m_lq else None
        value = {
            "quality": quality,
            "quality_pct": round(quality / 255 * 100) if quality is not None else None,
            "rssi_golden": int(m_rssi.group(1)) if m_rssi else None,
        }
    _signal_cache.update({"mac": mac, "at": now, "value": value})
    return value


def _bt_info_batch(macs: list[str]) -> dict[str, dict]:
    """Query `info` for many devices through a single bluetoothctl process."""
    if not macs:
        return {}
    cmds = "".join(f"info {mac}\n" for mac in macs)
    proc = subprocess.run(
        ["bluetoothctl"],
        input=cmds,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=15 + len(macs),
        check=False,
    )
    out = _ANSI_RE.sub("", proc.stdout or "")
    infos: dict[str, dict] = {}
    current: dict | None = None
    for raw in out.splitlines():
        line = _PROMPT_RE.sub("", raw).strip()
        m = _INFO_HEADER.match(line)
        if m:
            current = infos.setdefault(
                m.group(1).upper(),
                {
                    "paired": False,
                    "connected": False,
                    "trusted": False,
                    "name": None,
                    "alias": None,
                    "icon": None,
                    "audio": False,
                    "rssi": None,
                },
            )
            continue
        if current is None:
            continue
        if line.startswith("Name:"):
            current["name"] = display(line[5:], 80) or None  # advertised by the device: any bytes
        elif line.startswith("Alias:"):
            current["alias"] = line[6:].strip() or None
        elif line.startswith("Icon:"):
            current["icon"] = line[5:].strip() or None
        elif line.startswith("Paired:"):
            current["paired"] = line.endswith("yes")
        elif line.startswith("Trusted:"):
            current["trusted"] = line.endswith("yes")
        elif line.startswith("Connected:"):
            current["connected"] = line.endswith("yes")
        elif line.startswith("RSSI:"):
            # Present only while discovering: "RSSI: -55" or "RSSI: 0x.. (-55)".
            # Fallback is a full-field match so an unexpected format (e.g.
            # bare hex) yields no RSSI instead of a bogus trailing digit.
            m_rssi = re.search(r"\((-?\d+)\)\s*$", line) or re.fullmatch(
                r"RSSI:\s*(-?\d+)", line
            )
            if m_rssi:
                current["rssi"] = int(m_rssi.group(1))
        elif line.startswith("UUID:"):
            if "0000110b" in line.lower() or "Audio Sink" in line:
                current["audio"] = True
    for info in infos.values():
        if info["icon"] in _AUDIO_ICONS:
            info["audio"] = True
    return infos


def _device_from_line(mac: str, name: str, tags: str = "") -> dict:
    tags_u = tags.upper()
    return {
        "mac": mac.upper(),
        "name": display(name, 80),
        "paired": "PAIRED" in tags_u,
        "connected": "CONNECTED" in tags_u,
        "trusted": False,
        "primary": False,
    }


def _known_devices() -> dict:
    """App-level memory of past connects (written by the shell scripts)."""
    try:
        data = json.loads(KNOWN_DEVICES_FILE.read_text())
        devices = data.get("devices", {})
        return devices if isinstance(devices, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _enrich_devices(devices: list[dict]) -> list[dict]:
    """Merge batched bluetoothctl info: real names, audio capability, state."""
    infos = _bt_info_batch([d["mac"] for d in devices])
    primary = (_cfg_get("bluetooth.primary_mac") or "").upper()
    known = _known_devices()
    for d in devices:
        info = infos.get(d["mac"], {})
        listed = d["name"] if not _PLACEHOLDER_NAME.match(d["name"]) else None
        alias = info.get("alias")
        if alias and _PLACEHOLDER_NAME.match(alias):
            alias = None
        name = info.get("name") or alias or listed
        d["name"] = name or "Unknown device"
        d["unnamed"] = name is None
        d["paired"] = d["paired"] or bool(info.get("paired"))
        d["connected"] = d["connected"] or bool(info.get("connected"))
        d["trusted"] = bool(info.get("trusted"))
        d["icon"] = info.get("icon")
        d["audio"] = bool(info.get("audio"))
        d["rssi"] = info.get("rssi")
        d["last_connected"] = (known.get(d["mac"]) or {}).get("last_connected")
        d["primary"] = d["mac"] == primary
    devices.sort(
        key=lambda d: (
            not d["connected"],
            not d["primary"],
            not d["paired"],
            not d["audio"],
            d["unnamed"],
            d["name"].lower(),
        )
    )
    return devices


def _parse_bluetoothctl_devices(stdout: str) -> list[dict]:
    devices: list[dict] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        m = _DEVICE_LINE.match(line.strip())
        if not m:
            continue
        mac, name = m.group(1), m.group(2)
        if mac in seen:
            continue
        seen.add(mac)
        devices.append(_device_from_line(mac, name))
    return devices


def _parse_scan_script(stdout: str) -> list[dict]:
    devices: list[dict] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        m = _SCAN_LINE.match(line.strip())
        if not m:
            continue
        mac, name, tags = m.group(1), m.group(2), m.group(3) or ""
        if mac in seen:
            continue
        seen.add(mac)
        devices.append(_device_from_line(mac, name, tags))
    return devices


def list_devices() -> list[dict]:
    proc = subprocess.run(
        ["bluetoothctl", "devices"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=15,
        check=False,
    )
    return _enrich_devices(_parse_bluetoothctl_devices(proc.stdout or ""))


def scan_devices(seconds: int = 12) -> dict:
    seconds = max(5, min(60, seconds))
    proc = _run_script("bt_pair.sh", "scan", "--seconds", str(seconds), timeout=seconds + 45)
    devices = _parse_scan_script(proc.stdout or "")
    if devices:
        devices = _enrich_devices(devices)
    else:
        devices = list_devices()
    return {
        "ok": proc.returncode == 0,
        "seconds": seconds,
        "devices": devices,
        "stderr": (proc.stderr or "").strip() or None,
    }


def pair_device(mac: str) -> dict:
    proc = _run_script("bt_pair.sh", "pair", mac, timeout=120)
    # bt_codec_prime.sh at the end of the script already wrote bt_status.json;
    # re-run the slow refresh when file state contradicts the outcome OR the
    # file still points at a previously-connected device (stale on prime
    # failure — comparing only the connected bool would accept it).
    status = _read_bt_status()
    target = mac.strip().upper().replace("-", ":")
    mac_matches = (status.get("mac") or "").upper() == target
    if not mac_matches or bool(status.get("connected")) != (proc.returncode == 0):
        status = _refresh_bt_status()
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "bt": normalize_bt_status(status),
        "primary_mac": _cfg_get("bluetooth.primary_mac"),
    }


def remove_device(mac: str) -> dict:
    mac = mac.upper().replace("-", ":")
    if not _MAC_RE.match(mac):
        return {"ok": False, "detail": f"invalid MAC: {mac}"}
    proc = _run_script("bt_pair.sh", "remove", mac, timeout=30)
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "bt": normalize_bt_status(_read_bt_status()),
        "primary_mac": _cfg_get("bluetooth.primary_mac"),
    }


_scan_proc: subprocess.Popen | None = None


def _mark_scan_active(seconds: int) -> None:
    """Tell the bt-retry timer to skip its tick until the scan window ends.
    The sentinel stores a deadline epoch so it self-expires — a crashed scan
    can't wedge auto-reconnect. +3 s covers name-resolution after inquiry."""
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        SCAN_SENTINEL.write_text(str(int(time.time()) + seconds + 3))
    except OSError:
        pass


def start_scan(seconds: int = 25) -> dict:
    """Kick off background discovery; the UI polls /api/bt/devices while
    BlueZ fills its cache (devices appear live instead of after the window).
    Classic BR/EDR speakers take ~8-16 s to surface (inquiry + remote-name
    resolve), so the window must be comfortably longer than that."""
    global _scan_proc
    seconds = max(5, min(60, seconds))
    # Refresh the retry-backoff deadline even if a scan is already running,
    # so re-clicking Scan extends the window instead of letting the timer in.
    _mark_scan_active(seconds)
    if _scan_proc is not None and _scan_proc.poll() is None:
        return {"ok": True, "seconds": seconds, "already": True}
    subprocess.run(
        ["bluetoothctl", "power", "on"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    _scan_proc = subprocess.Popen(
        ["bluetoothctl", "--timeout", str(seconds), "scan", "on"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"ok": True, "seconds": seconds}


# Canonical codec names accepted by bluealsa-cli / bt_codec_select.sh
_CODEC_NAMES = {
    "sbc": "SBC",
    "sbc-xq": "SBC-XQ",
    "sbcxq": "SBC-XQ",
    "aac": "AAC",
    "aptx": "aptX",
    "aptx-hd": "aptX-HD",
    "aptxhd": "aptX-HD",
    "ldac": "LDAC",
}


def select_codec(codec: str, mac: str | None = None) -> dict:
    name = _CODEC_NAMES.get(codec.strip().lower())
    if not name:
        return {"ok": False, "detail": f"unknown codec: {codec}"}
    mac = (mac or _cfg_get("bluetooth.primary_mac") or "").upper().replace("-", ":")
    if not _MAC_RE.match(mac):
        return {"ok": False, "detail": f"invalid MAC: {mac or '(none)'}"}
    proc = _run_script("bt_codec_select.sh", mac, name, timeout=90)
    bt = normalize_bt_status(_read_bt_status())
    return {
        # SBC negotiated in dual-channel HD mode is reported as "SBC-XQ"
        "ok": proc.returncode == 0 and bt.get("codec") in (name, f"{name}-XQ"),
        "requested": name,
        "returncode": proc.returncode,
        "stderr": (proc.stderr or "").strip() or None,
        "bt": bt,
    }


def set_primary(mac: str) -> dict:
    mac = mac.upper().replace("-", ":")
    if not _MAC_RE.match(mac):
        return {"ok": False, "detail": f"invalid MAC: {mac}"}
    _cfg_set("bluetooth.primary_mac", mac)
    connect = _run_script("bt_connect.sh", mac, timeout=90)
    restart = subprocess.run(
        ["systemctl", "restart", "audio-player-mpv.service"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
        check=False,
    )
    bt = normalize_bt_status(_read_bt_status())
    ok = connect.returncode == 0 and restart.returncode == 0 and bt.get("connected")
    return {
        "ok": ok,
        "primary_mac": mac,
        "connect_rc": connect.returncode,
        "mpv_restart_rc": restart.returncode,
        "stderr": (connect.stderr or restart.stderr or "").strip() or None,
        "bt": bt,
    }
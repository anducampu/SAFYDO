"""System helpers — BT status, script execution."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from pathlib import Path

from backend.config import INSTALL_DIR, cfg_get
from backend.services.bluetooth import link_signal, normalize_bt_status, watchdog_info
from backend.services.bluez import device_connected
from backend.services.scripts import last_error, run_script
from backend.services.volume_hw import read_hw_volume

STATE_DIR = Path("/var/lib/audio-player")
BT_STATUS_FILE = STATE_DIR / "bt_status.json"

_RECONCILE_INTERVAL = 15.0
_reconcile_lock = threading.Lock()
_last_reconcile = 0.0

# Called (from a worker thread) when the reconciler sees the device come
# back; main.py registers a hook that resumes interrupted playback.
_bt_reconnect_hook = None


def set_bt_reconnect_hook(hook) -> None:
    global _bt_reconnect_hook
    _bt_reconnect_hook = hook


def _reconcile_bt_status(cached: dict) -> None:
    """Detect device-initiated connects/drops the status file never sees.

    bt_status.json is only written by the connect/prime scripts, so when the
    device itself (re)connects or the link dies, the UI shows stale state
    forever. Every _RECONCILE_INTERVAL compare the file against bluetoothctl
    and refresh in the background on mismatch.
    """
    global _last_reconcile
    mac = cached.get("mac") or str(cfg_get("bluetooth.primary_mac") or "").strip()
    if not mac:
        return
    now = time.monotonic()
    with _reconcile_lock:
        if now - _last_reconcile < _RECONCILE_INTERVAL:
            return
        _last_reconcile = now
    known = device_connected(mac)
    if known is None:
        return  # bluetoothd restarting / device unknown: nothing to reconcile against
    really_connected = known
    if really_connected != bool(cached.get("connected")):

        def _refresh_and_notify() -> None:
            refresh_bt_status()
            hook = _bt_reconnect_hook
            if really_connected and hook is not None:
                try:
                    hook()
                except Exception:  # noqa: BLE001 — never kill the worker
                    pass

        threading.Thread(target=_refresh_and_notify, daemon=True).start()


def read_bt_status() -> dict:
    status = normalize_bt_status({"connected": False, "codec": None, "device": None})
    if BT_STATUS_FILE.exists():
        try:
            status = normalize_bt_status(json.loads(BT_STATUS_FILE.read_text()))
        except json.JSONDecodeError:
            pass
    _reconcile_bt_status(status)
    # Link quality/RSSI are NOT read here any more: this runs on every 1 Hz
    # status tick, and each reading is two HCI commands to a controller that is
    # already saturated by A2DP (it wedges under that load; see docs/BUILD.md).
    # The Bluetooth panel asks GET /api/bt/signal while it is actually showing.
    status["signal"] = None
    # Attached here, after the reconcile that may rewrite the file, for the same
    # reason as signal: it is live state, not something bt_status.json knows.
    # Every reader gets it — the REST endpoint and the 1 Hz status frame the
    # Bluetooth panel actually renders — so a controller that has stopped
    # answering cannot look like a speaker that is simply switched off.
    status["watchdog"] = watchdog_info()
    return status


def refresh_bt_status() -> dict:
    script = INSTALL_DIR / "scripts" / "bt_codec.sh"
    if script.is_file():
        subprocess.run(
            [str(script), "--update-status"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    return read_bt_status()




_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def valid_hostname(name: str) -> str:
    """RFC 1123 label in lower case — the same rule as valid_hostname() in
    scripts/hostname_conflict.sh. Returns the normalised name or raises."""
    name = (name or "").strip().lower()
    if name.endswith(".local"):
        name = name[: -len(".local")]
    if not _HOSTNAME_RE.match(name):
        raise ValueError(
            "Use a-z, 0-9 and '-' (1-63 characters), not starting or ending with '-'"
        )
    if name.isdigit():
        raise ValueError("A name made only of digits reads as an address")
    if name == "localhost":
        raise ValueError("'localhost' is reserved")
    return name


def set_hostname(name: str, force: bool = False) -> dict:
    """Rename the box (hostname + /etc/hosts + avahi + player.yaml) through
    hostname_conflict.sh, which also checks nobody else on the LAN answers
    the new name.local."""
    from backend.config import cfg_get as _cfg_get

    name = valid_hostname(name)
    args = ["set", name] + (["--force"] if force else [])
    proc = run_script("hostname_conflict.sh", *args, timeout=60)
    _cfg_get.cache_clear()  # the script wrote player.yaml behind our cache
    if proc.returncode != 0:
        raise ValueError(last_error(proc, "rename failed"))
    return {"hostname": name, "mdns": f"{name}.local", "url": f"http://{name}.local/"}


def bt_connect(mac: str | None = None) -> dict:
    args = [mac] if mac else []
    proc = run_script("bt_connect.sh", *args, timeout=60)
    # bt_codec_prime.sh inside the script already wrote bt_status.json on
    # success; re-run the slow refresh when file state contradicts the
    # outcome OR still points at a different device (stale on prime failure).
    status = read_bt_status()
    target = (mac or str(cfg_get("bluetooth.primary_mac") or "")).strip().upper().replace("-", ":")
    mac_matches = not target or (status.get("mac") or "").upper() == target
    if not mac_matches or bool(status.get("connected")) != (proc.returncode == 0):
        status = refresh_bt_status()
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "bt": status,
        "primary_mac": cfg_get("bluetooth.primary_mac"),
    }


def _light_audio_check(player) -> dict:
    """Cheap status for WebSocket — no subprocess probes."""
    bt = read_bt_status()
    mac = str(cfg_get("bluetooth.primary_mac") or "").strip()
    failures: list[str] = []
    if not mac:
        failures.append("primary_mac_empty")
    elif not bt.get("connected"):
        failures.append("bluetooth_not_connected")
    if not player.mpv.available():
        failures.append("mpv_socket_missing")
    return {"ok": len(failures) == 0, "failures": failures, "warnings": []}


def full_status(player, *, deep_check: bool = False) -> dict:
    player.sync_hw_volume_if_enabled()
    payload: dict = {
        "player": player.status(),
        "bluetooth": read_bt_status(),
        "primary_mac": cfg_get("bluetooth.primary_mac"),
        "radio": player.radio.summary() if player.radio is not None else None,
    }
    if deep_check:
        from backend.services.audio_verify import verify_audio

        payload["audio_check"] = verify_audio(player.mpv, cfg_get, INSTALL_DIR)
    else:
        payload["audio_check"] = _light_audio_check(player)
    return payload
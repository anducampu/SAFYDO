"""Bluetooth AVRCP absolute volume via org.bluez.MediaTransport1."""

from __future__ import annotations

import re
import subprocess
import time

from backend.services import dbus_bluez

_UINT16_RE = re.compile(r"uint16\s+(\d+)")


def _transport_candidates(mac: str) -> list[str]:
    # BlueZ numbers transports per connection (fd0, fd1, ... fd5 after a few
    # reconnects) and doesn't always nest them under a sepN, so a guessed list
    # of names went blind and the slider silently
    # stopped reaching the speaker. List what exists instead.
    base = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}/"
    if dbus_bluez.available():
        try:
            objs = dbus_bluez.managed_objects()
            return [p for p, ifaces in objs.items() if p.startswith(base) and dbus_bluez.TRANSPORT in ifaces]
        except Exception:  # noqa: BLE001 - fall back to the subprocess below
            pass
    proc = subprocess.run(
        ["busctl", "--list", "tree", "org.bluez"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=5,
        check=False,
    )
    return [
        line.strip()
        for line in (proc.stdout or "").splitlines()
        # Either .../sepN/fdN or straight under the device (.../fdN): the Go 3's
        # transport sat at dev_.../fd2, the KALLSUP's at dev_.../sep1/fd3.
        if line.strip().startswith(base) and re.search(r"/fd\d+$", line.strip())
    ]


def _get_volume_raw(path: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "dbus-send",
            "--system",
            "--dest=org.bluez",
            "--print-reply",
            path,
            "org.freedesktop.DBus.Properties.Get",
            "string:org.bluez.MediaTransport1",
            "string:Volume",
        ],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=5,
        check=False,
    )


# A cached volume is trusted for this long without asking BlueZ, as long as the
# signal listener is up (it rewrites the entry on every change).
_VOLUME_CACHE_TTL = 30.0


def _volume_of(path: str) -> int | None:
    """AVRCP volume 0-127 of a transport, or None when it has none / is gone."""
    if dbus_bluez.available():
        dbus_bluez.ensure_listener()
        hit = dbus_bluez.volume_cache.get(path)
        if hit and dbus_bluez.listener_alive() and time.monotonic() - hit[0] < _VOLUME_CACHE_TTL:
            return hit[1]
        try:
            vol = int(dbus_bluez.get_property(path, dbus_bluez.TRANSPORT, "Volume"))
        except Exception:  # noqa: BLE001 - no Volume, transport gone, bus hiccup
            dbus_bluez.volume_cache.pop(path, None)
            return None
        dbus_bluez.volume_cache[path] = (time.monotonic(), vol)
        return vol
    proc = _get_volume_raw(path)
    if proc.returncode != 0:
        return None
    m = _UINT16_RE.search(proc.stdout or "")
    return int(m.group(1)) if m else None


def _probe_transport_path(mac: str) -> str | None:
    for path in _transport_candidates(mac):
        if _volume_of(path) is not None:
            return path
    return None


def speaker_has_remote_volume(mac: str | None) -> bool | None:
    """False when the speaker is connected but BlueZ offers no volume for it.

    The transport exists without a Volume property when the speaker never
    negotiated AVRCP absolute volume: nothing the Pi sends reaches its amplifier.
    None when there is no transport to ask (not connected, not streaming).
    """
    if not mac:
        return None
    mac = mac.upper()
    hit = _remote_volume_cache.get(mac)
    if hit and time.monotonic() - hit[0] < 10:
        return hit[1]
    try:
        if transport_path_for_mac(mac):
            value: bool | None = True
        else:
            value = False if _transport_candidates(mac) else None
    except (OSError, subprocess.SubprocessError):
        value = None
    _remote_volume_cache[mac] = (time.monotonic(), value)
    return value


_remote_volume_cache: dict[str, tuple[float, bool | None]] = {}


# mac -> (found_at, path). A hit is good until the transport goes (a failed read
# drops it); a miss is retried soon, or a lookup made in the second before a
# reconnect finished blinded the slider for the rest of a 30 s window.
_HIT_TTL = 30.0
_MISS_TTL = 3.0
_path_cache: dict[str, tuple[float, str | None]] = {}


def _forget_transport_paths() -> None:
    _path_cache.clear()


def transport_path_for_mac(mac: str) -> str | None:
    mac = mac.upper()
    now = time.monotonic()
    hit = _path_cache.get(mac)
    if hit and now - hit[0] < (_HIT_TTL if hit[1] else _MISS_TTL):
        return hit[1]
    path = _probe_transport_path(mac)
    _path_cache[mac] = (now, path)
    return path


def hw_to_ui(vol127: int) -> int:
    return max(0, min(100, round(vol127 * 100 / 127)))


def ui_to_hw(vol100: int) -> int:
    return max(0, min(127, round(vol100 * 127 / 100)))


def read_hw_volume(mac: str | None) -> int | None:
    if not mac:
        return None
    try:
        return _read_volume(mac)
    except (OSError, subprocess.SubprocessError):
        return None


def _read_volume(mac: str) -> int | None:
    path = transport_path_for_mac(mac.upper())
    if not path:
        return None
    vol = _volume_of(path)
    if vol is None:
        # The transport went (reconnect = new fdN): find the new one next time
        # instead of serving the dead path for the rest of the 30 s bucket.
        _forget_transport_paths()
        return None
    return hw_to_ui(vol)


def set_hw_volume(mac: str | None, level: int) -> bool:
    if not mac:
        return False
    try:
        path = transport_path_for_mac(mac.upper())
    except (OSError, subprocess.SubprocessError):
        return False
    if not path:
        return False
    hw = ui_to_hw(level)
    try:
        return _write_volume(path, hw)
    except (OSError, subprocess.SubprocessError):
        return False


def _write_volume(path: str, hw: int) -> bool:
    if dbus_bluez.available():
        try:
            dbus_bluez.set_property(path, dbus_bluez.TRANSPORT, "Volume", "q", hw)
        except Exception:  # noqa: BLE001
            return False
        dbus_bluez.volume_cache[path] = (time.monotonic(), hw)
        return True
    proc = subprocess.run(
        [
            "dbus-send",
            "--system",
            # Not optional: without it dbus-send emits a signal rather than making
            # a method call. BlueZ ignores that, and the exit status is still 0.
            "--print-reply",
            "--dest=org.bluez",
            path,
            "org.freedesktop.DBus.Properties.Set",
            "string:org.bluez.MediaTransport1",
            "string:Volume",
            f"variant:uint16:{hw}",
        ],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=5,
        check=False,
    )
    return proc.returncode == 0
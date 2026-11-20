"""Read BlueZ device state straight off D-Bus.

`bluetoothctl info` registers an agent client and an advertisement monitor with
bluetoothd on every launch; polled, that churn is what once stole the pairing
agent mid-pair. busctl asks the one property and leaves. Mirrors bt_dev_prop in
scripts/_common.sh.
"""

from __future__ import annotations

import subprocess

from backend.services import dbus_bluez


def device_prop(mac: str | None, prop: str) -> str | None:
    """'true'/'false' for booleans, the bare string otherwise; None if unknown."""
    if not mac:
        return None
    path = f"/org/bluez/hci0/dev_{mac.upper().replace(':', '_')}"
    if dbus_bluez.available():
        try:
            value = dbus_bluez.get_property(path, "org.bluez.Device1", prop)
        except Exception:  # noqa: BLE001 - unknown device, bluetoothd restarting
            return None
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)
    try:
        proc = subprocess.run(
            ["busctl", "get-property", "org.bluez", path, "org.bluez.Device1", prop],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or " " not in out:
        return None
    kind, value = out.split(" ", 1)
    if kind == "s" and len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def device_connected(mac: str | None) -> bool | None:
    """None when BlueZ couldn't be asked (unknown device, bluetoothd restarting)."""
    v = device_prop(mac, "Connected")
    return None if v is None else v == "true"

"""Persistent system-bus access to BlueZ (python3-jeepney), no subprocesses.

Before this, every speaker-volume read forked `dbus-send` (~1/s while playing)
and every transport lookup forked `busctl --list tree org.bluez`, which dumps
bluetoothd's whole object tree: on the 3 A+ (2026-11-20) that was 2-3 new
processes and a fresh D-Bus client every second, dbus-daemon at 1.5-3 % CPU.

Two connections:
  * a shared one for method calls (Get/Set/GetManagedObjects), under a lock,
    reopened on failure;
  * a listener thread on its own connection, subscribed to the transports'
    PropertiesChanged and to InterfacesRemoved, keeping `volume_cache` current
    so a volume read normally costs no D-Bus call at all.

`available()` is False when jeepney is missing; callers then keep their old
subprocess path, so an install without the package still works.
"""

from __future__ import annotations

import threading
import time
from typing import Any

try:  # python3-jeepney (apt); optional so an older install keeps working
    from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
    from jeepney.io.blocking import Proxy, open_dbus_connection
    from jeepney.wrappers import unwrap_msg

    _HAVE_JEEPNEY = True
except ImportError:  # pragma: no cover - depends on the board
    _HAVE_JEEPNEY = False

BLUEZ = "org.bluez"
PROPS = "org.freedesktop.DBus.Properties"
TRANSPORT = "org.bluez.MediaTransport1"
CALL_TIMEOUT = 5.0

_lock = threading.Lock()
_conn = None

# path -> (monotonic time of the update, AVRCP volume 0-127)
volume_cache: dict[str, tuple[float, int]] = {}
_listener_started = False
_listener_alive = False


def available() -> bool:
    return _HAVE_JEEPNEY


def listener_alive() -> bool:
    return _listener_alive


def _connection():
    global _conn
    if _conn is None:
        _conn = open_dbus_connection(bus="SYSTEM")
    return _conn


def _drop_connection() -> None:
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:  # noqa: BLE001
            pass
    _conn = None


def _call(path: str, interface: str, method: str, signature: str | None = None, body: tuple = ()) -> tuple:
    """One method call on the shared connection. Raises on a D-Bus error reply
    or a dead bus; the connection is reopened on the next call."""
    if not _HAVE_JEEPNEY:
        raise OSError("python3-jeepney not installed")
    addr = DBusAddress(path, bus_name=BLUEZ, interface=interface)
    msg = new_method_call(addr, method, signature, body)
    if not _lock.acquire(timeout=CALL_TIMEOUT):
        raise TimeoutError("D-Bus connection busy")
    try:
        try:
            reply = _connection().send_and_get_reply(msg, timeout=CALL_TIMEOUT)
        except Exception:
            _drop_connection()
            raise
    finally:
        _lock.release()
    return unwrap_msg(reply)


def get_property(path: str, interface: str, prop: str) -> Any:
    (variant,) = _call(path, PROPS, "Get", "ss", (interface, prop))
    return variant[1]


def set_property(path: str, interface: str, prop: str, signature: str, value: Any) -> None:
    _call(path, PROPS, "Set", "ssv", (interface, prop, (signature, value)))


def managed_objects() -> dict[str, dict[str, dict[str, Any]]]:
    """path -> interface -> property -> plain value (variants unwrapped)."""
    (objs,) = _call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
    return {
        path: {iface: {k: v[1] for k, v in props.items()} for iface, props in ifaces.items()}
        for path, ifaces in objs.items()
    }


def ensure_listener() -> None:
    """Start the signal listener once (idempotent, cheap to call)."""
    global _listener_started
    if not _HAVE_JEEPNEY or _listener_started:
        return
    _listener_started = True
    threading.Thread(target=_listen_forever, name="bluez-signals", daemon=True).start()


def _listen_forever() -> None:
    global _listener_alive
    backoff = 1.0
    while True:
        try:
            _listen_once()
            backoff = 1.0
        except Exception:  # noqa: BLE001 - bluetoothd restart, bus hiccup
            pass
        _listener_alive = False
        # The cache may have missed changes while we were away.
        volume_cache.clear()
        time.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


def _listen_once() -> None:
    global _listener_alive
    conn = open_dbus_connection(bus="SYSTEM")
    try:
        bus = Proxy(message_bus, conn)

        def rules(**sender):
            changed = MatchRule(
                type="signal", interface=PROPS, member="PropertiesChanged",
                path_namespace="/org/bluez", **sender,
            )
            changed.add_arg_condition(0, TRANSPORT)
            removed = MatchRule(
                type="signal", interface="org.freedesktop.DBus.ObjectManager",
                member="InterfacesRemoved", **sender,
            )
            return changed, removed

        # The daemon resolves sender=org.bluez to bluetoothd; the local filter
        # cannot (signals carry the unique name, ":1.x"), so it matches without.
        for rule in rules(sender=BLUEZ):
            bus.AddMatch(rule)
        local_changed, local_removed = rules()
        with conn.filter(local_changed) as q_changed, conn.filter(local_removed) as q_removed:
            _listener_alive = True
            while True:
                # A timeout only bounds how late a removal is applied (a failed
                # read drops a dead path anyway); a dead socket raises.
                try:
                    _on_changed(conn.recv_until_filtered(q_changed, timeout=10))
                except TimeoutError:
                    pass
                while q_removed:
                    _on_removed(q_removed.popleft())
    finally:
        conn.close()


def _on_changed(msg) -> None:
    try:
        _iface, changed, invalidated = msg.body
    except (TypeError, ValueError):
        return
    path = msg.header.fields.get(1)  # HeaderFields.path
    if not path:
        return
    if "Volume" in changed:
        volume_cache[path] = (time.monotonic(), int(changed["Volume"][1]))
    elif "Volume" in invalidated:
        volume_cache.pop(path, None)


def _on_removed(msg) -> None:
    try:
        path, ifaces = msg.body
    except (TypeError, ValueError):
        return
    if TRANSPORT in ifaces or any(p.startswith(path + "/") for p in volume_cache):
        for p in [p for p in volume_cache if p == path or p.startswith(path + "/")]:
            volume_cache.pop(p, None)

#!/usr/bin/env python3
"""Watch for A2DP stalls and record what every layer was doing at the time.

The stall on the Pi 3 A+ shows up as: mpv's audio clock freezes while its cache
keeps growing, `bluealsa-cli` still reports the transport `Running: true`, and
btmon sees no ACL packets at all. Each of those is a different tool, so nobody
had them on one clock. This puts them on one clock.

One line per second:

  ts pos=<mpv time-pos> d=<delta> cache=<demuxer cache s> idle=<core-idle>
  acl_tx=<packets/s> acl_rx=<packets/s> acl=<counter health>
  lq=<0-255> rssi=<golden> run=<transport>
  if=<default route iface> gw=<ok|MISS>

plus `*** STALL` / `*** RESUMED` events, so a dropout can be read straight out of
the log without replaying anything. Also logs adapter swaps, which is how a
network change gets correlated with an audio stall rather than guessed at.

Root, for hcitool and the mpv IPC socket:
  sudo systemd-run --unit=safydo-stallwatch --collect \\
      /opt/rpi-audio-player/scripts/bt_stall_watch.py
  journalctl -u safydo-stallwatch -f     # or tail the log file
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

MPV_SOCK = "/run/audio-player/mpv.sock"
DEFAULT_LOG = "/var/tmp/safydo-stall.log"
SLOW_EVERY = 5          # seconds between the subprocess-backed samples
STALL_AFTER = 2         # consecutive frozen samples before calling it a stall
DOWN_CONFIRM_S = 10.0   # hci0 down this long = controller fault, not a service restart

_ACL_TX = re.compile(r"ACL Data TX")
_ACL_RX = re.compile(r"ACL Data RX")
_LQ = re.compile(r"Link quality:\s*(\d+)")
_RSSI = re.compile(r"RSSI return value:\s*(-?\d+)")


def _run(cmd: list[str], timeout: float = 3.0) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout or ""


class AclCounter:
    """ACL packet counters, taken from the kernel rather than from a pipe.

    This started out parsing `btmon`, and twice it lied: the counter sat at zero
    for the best part of an hour while an independent btmon, run at the same
    moment, saw 108 packets/s. The btmon process was alive, its reader thread was
    alive, and it was still burning 5 % CPU on packets — the output simply never
    reached us. A witness with a pipe, a subprocess, a decoder and a thread has
    four ways to go quiet, and every one of them looks exactly like the stall
    being hunted.

    `hciconfig` reads the same counters the kernel keeps for the adapter
    (HCIGETDEVINFO). One exec per tick, no state of our own, and nothing between
    the number and its source. Verified against btmon: 110/s vs 108/s.
    """

    _ACL = re.compile(r"^\s*(RX|TX) bytes:\d+\s+acl:(\d+)", re.MULTILINE)

    def __init__(self, dev: str = "hci0") -> None:
        self.dev = dev

    def start(self) -> None:  # kept for symmetry with the old interface
        return None

    def totals(self) -> tuple[int | None, int | None, bool | None]:
        """(tx, rx, up) — cumulative ACL packets and whether the adapter is up.

        The adapter state comes from the same exec because it is now the primary
        signature: on 2026-08-08 the controller itself wedged mid-stream (HCI
        reset timing out, hci0 DOWN and unpowered from that second on), which is
        what "bluealsa stopped feeding the transport" had been all along.
        """
        raw = _run(["hciconfig", self.dev])
        if not raw:
            return None, None, None
        rx = tx = None
        for direction, count in self._ACL.findall(raw):
            if direction == "TX":
                tx = int(count)
            else:
                rx = int(count)
        up = None
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith("UP"):
                up = True
            elif stripped.startswith("DOWN"):
                up = False
        return tx, rx, up


class MpvIpc:
    """Minimal mpv JSON IPC client that reconnects on its own."""

    def __init__(self, path: str = MPV_SOCK) -> None:
        self.path = path
        self.sock: socket.socket | None = None
        self.buf = b""
        self.rid = 0

    def _connect(self) -> bool:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect(self.path)
        except OSError:
            self.sock = None
            return False
        self.sock = s
        self.buf = b""
        return True

    def get(self, prop: str):
        if self.sock is None and not self._connect():
            return None
        self.rid += 1
        rid = self.rid
        try:
            assert self.sock
            self.sock.sendall(
                json.dumps({"command": ["get_property", prop], "request_id": rid}).encode()
                + b"\n"
            )
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                while b"\n" in self.buf:
                    line, self.buf = self.buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    # Events are interleaved with replies; only ours matters.
                    if msg.get("request_id") == rid:
                        return msg.get("data") if msg.get("error") == "success" else None
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self.buf += chunk
        except (OSError, AssertionError):
            pass
        self.sock = None
        return None


def bt_mac() -> str | None:
    for line in _run(["bluetoothctl", "devices", "Connected"]).splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "Device":
            return parts[1]
    return None


def pcm_path() -> str | None:
    for line in _run(["bluealsa-cli", "list-pcms"]).splitlines():
        if line.strip().endswith("/sink"):
            return line.strip()
    return None


def transport_running(pcm: str | None) -> str:
    if not pcm:
        return "-"
    for line in _run(["bluealsa-cli", "info", pcm]).splitlines():
        if line.startswith("Running:"):
            return line.split(":", 1)[1].strip()
    return "-"


def default_iface() -> tuple[str, str]:
    """(iface, gateway) from the live default route."""
    for line in _run(["ip", "-4", "route", "show", "default"]).splitlines():
        f = line.split()
        gw = f[f.index("via") + 1] if "via" in f else "-"
        dev = f[f.index("dev") + 1] if "dev" in f else "-"
        return dev, gw
    return "-", "-"


def gateway_ok(gw: str, dev: str) -> bool:
    if gw == "-" or dev == "-":
        return False
    try:
        return subprocess.run(
            ["ping", "-c1", "-W1", "-n", "-q", "-I", dev, gw],
            capture_output=True,
            timeout=3,
            check=False,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args()

    log = Path(args.log)
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = log.open("a", buffering=1)  # line buffered: a hard reset keeps the tail

    def emit(line: str) -> None:
        stamp = datetime.now().astimezone().strftime("%H:%M:%S")
        fh.write(f"{stamp} {line}\n")
        print(f"{stamp} {line}", flush=True)

    acl = AclCounter()
    mpv = MpvIpc()

    emit(f"=== watch start (pid {os.getpid()}) log={log} ===")

    pcm = pcm_path()
    mac = bt_mac()
    last_tx, last_rx, hci_was_up = acl.totals()
    last_pos: float | None = None
    frozen = 0
    down_since: float | None = time.monotonic() if hci_was_up is False else None
    down_reported = False
    stalling = False
    stall_started = 0.0
    lq = rssi = "-"
    running = "-"
    iface, gw = default_iface()
    gw_state = "?"
    tick = 0

    while True:
        started = time.monotonic()
        tick += 1

        pos = mpv.get("time-pos")
        cache = mpv.get("demuxer-cache-time")
        idle = mpv.get("core-idle")
        tx, rx, hci_up = acl.totals()
        acl_health = "ok" if tx is not None else "UNREADABLE"
        # Restarting bluetooth.service (every pairing, the connect flow's SEP
        # cache wipe) also takes hci0 down — for a second or two. A wedged
        # controller stays down until a reboot, so only a long outage is one.
        if hci_up is False and hci_was_up is not False:
            down_since = started
        if hci_up is False and down_since is not None and not down_reported \
                and started - down_since >= DOWN_CONFIRM_S:
            down_reported = True
            emit(
                f"*** CONTROLLER DOWN — hci0 down for {DOWN_CONFIRM_S:.0f}s. Kernel will show "
                "'command tx timeout' / 'Opcode 0x0c03 failed: -110'. Recover with "
                "scripts/bt_recover.sh (unbind, 5s, rebind — works on the Zero 2W, "
                "~16s, no reboot); on the 3 A+ that fails and a reboot is needed."
            )
        elif hci_up is True and hci_was_up is False:
            if down_reported:
                emit("*** CONTROLLER BACK — hci0 is up again")
            elif down_since is not None:
                emit(f"hci0 restarted (down {started - down_since:.0f}s) — bluetooth.service restart, not a controller fault")
            down_since = None
            down_reported = False
        hci_was_up = hci_up
        d_tx = tx - last_tx if (tx is not None and last_tx is not None) else 0
        d_rx = rx - last_rx if (rx is not None and last_rx is not None) else 0
        last_tx, last_rx = tx, rx

        if tick % SLOW_EVERY == 1:
            if mac:
                m = _LQ.search(_run(["hcitool", "lq", mac]))
                lq = m.group(1) if m else "-"
                m = _RSSI.search(_run(["hcitool", "rssi", mac]))
                rssi = m.group(1) if m else "-"
            else:
                mac = bt_mac()
            if not pcm:
                pcm = pcm_path()
            running = transport_running(pcm)
            new_iface, new_gw = default_iface()
            if (new_iface, new_gw) != (iface, gw):
                emit(f"*** LINK CHANGED {iface}/{gw} -> {new_iface}/{new_gw}")
                iface, gw = new_iface, new_gw
            gw_state = "ok" if gateway_ok(gw, iface) else "MISS"

        delta = None if pos is None or last_pos is None else pos - last_pos
        # A frozen clock only counts while mpv believes it is playing: idle is
        # true when it is paused or has nothing to do, and that is not a stall.
        #
        # "Not frozen" has to mean the clock genuinely moved forward. Treating
        # anything else as movement reported three false recoveries during the
        # 11:41 stall: mpv restarts mid-stall, so time-pos jumps back to zero and
        # briefly reads None, and both were being scored as the audio returning
        # while not a single packet had left the adapter.
        playing = idle is False
        advanced = delta is not None and 0.05 <= delta < 10
        if playing and not advanced:
            frozen += 1
        elif advanced:
            frozen = 0

        if frozen >= STALL_AFTER and not stalling:
            stalling = True
            stall_started = time.monotonic()
            # pos can be None here — mpv restarts mid-stall and time-pos reads
            # unavailable — and `None:.2f` raised, killing the watcher at the
            # one moment it exists for.
            at = "-" if pos is None else f"{pos:.2f}"
            emit(
                f"*** STALL clock frozen at {at}s — acl_tx={d_tx} acl_rx={d_rx} "
                f"acl={acl_health} run={running} lq={lq} rssi={rssi} "
                f"if={iface} gw={gw_state}"
            )
        elif stalling and frozen == 0:
            stalling = False
            at = "-" if pos is None else f"{pos:.2f}"
            emit(
                f"*** RESUMED after {time.monotonic() - stall_started:.1f}s at {at}s "
                f"— acl_tx={d_tx} hci={'up' if hci_up else 'DOWN'}"
            )

        emit(
            f"pos={'-' if pos is None else f'{pos:.2f}'} "
            f"d={'-' if delta is None else f'{delta:.2f}'} "
            f"cache={'-' if cache is None else f'{cache:.1f}'} "
            f"idle={idle} acl_tx={d_tx} acl_rx={d_rx} acl={acl_health} "
            f"hci={'up' if hci_up else 'DOWN' if hci_up is False else '?'} "
            f"lq={lq} rssi={rssi} run={running} if={iface} gw={gw_state}"
        )
        last_pos = pos

        time.sleep(max(0.0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)

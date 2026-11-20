"""Watch playback for an output that falls behind, and work out whose fault it is.

Two different faults look the same from here: mpv's clock advances by the audio
the sink consumed, and in both it advances by less than wall time.

1. A speaker that cannot keep up with SBC-XQ. A sink advertises dual channel
   and a bitpool, and nothing in that says whether its radio can actually drain
   the result. The IKEA KALLSUP advertises the same SBC capabilities as the JBL
   Go 3 and plays SBC-XQ ~13 % slower than real time, which is heard as stutter.
   Measured on the 3 A+ (2026-11-13): Go 3 behind in 2 of 234 one-second
   samples, KALLSUP in 67 of 227, and in 0 of 40 once on standard SBC.

2. Our own output stuck in a bad state, on any codec (2026-11-20, JBL Boombox
   3). mpv reported an underrun every 5.7 s, from boot onwards, through a
   bluealsa restart, a codec change and a station change. The host stopped
   sending for ~0.25 s each time (ACL TX fell to 0-20 packets/s from ~105) and
   the speaker played the gap as clicks and garble. No HCI command, process or
   scan lined up with the gaps; the period matches a 0.5 s buffer drained at
   48000/44100 (8.8 %). One mpv `ao-reload` cleared it at once. The trigger was
   not found, and a later cold reconnect did not bring it back. Because it
   looked like case 1, the old probe moved the Boombox to standard SBC, where
   the underruns carried on.

So the response escalates. The first bad window reopens mpv's audio output,
which is cheap (a ~0.2 s gap) and is the cure for case 2. Only if the next
window is still bad, on SBC-XQ, does the speaker get the blame:
bt_codec_select.sh --deny-xq records it and reconnects with bluealsa on
standard SBC (a live switch drops the link on both speakers tested). Later
connections start there, and Retry XQ in the UI clears the record.

If a reload does not cure it, the speaker is reconnected (resets both ends of
the stream). Reloads are limited to one per 10 min and reconnects to one per
30 min, so a fault neither cures cannot turn into a repair every 30 s.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from backend.config import INSTALL_DIR

BT_STATUS_FILE = Path("/var/lib/audio-player/bt_status.json")

SKIP_S = 8.0  # stream start-up: mpv waits for cache, a stutter here is not the sink
WINDOW = 30  # usable one-second samples per verdict
BEHIND = 0.95  # a second in which mpv's clock advanced less than this is "behind"
# A window this bad reopens the output. The stuck output scored 5-6 of 30 and a
# healthy Go 3 about 0.3, so this sits clear of both.
GLITCH_BEHIND = 3
MAX_BEHIND = 6  # SBC-XQ is blamed above this, and only after a reload. KALLSUP: ~9 of 30
MIN_CACHE_S = 2.0  # a network stream this low on cache may be starving, not the sink
RELOAD_COOLDOWN_S = 600.0  # at most one output reload per connection per 10 min
# When a reload did not cure it, reconnect the speaker: that resets both ends of
# the stream, and is what brought the Boombox back on 2026-11-20. Rarer still,
# because it is a ~10 s gap.
RECONNECT_COOLDOWN_S = 1800.0


class XqProbe:
    def __init__(self, mpv, on_reconnected=None) -> None:
        self.mpv = mpv
        # The verdict reconnects the speaker (bluealsa restarts in the other
        # mode), so playback stops; this brings it back.
        self.on_reconnected = on_reconnected
        self._key: tuple | None = None  # (mac, codec) of the connection being watched
        self._xq_judged = False  # SBC-XQ verdict given for this connection
        self._reloaded_at: float | None = None  # monotonic, last ao-reload for this connection
        self._reconnected_at: float | None = None  # monotonic, last reconnect by the probe
        self._reload_failed_logged = False
        self._healthy_logged = False
        self._playing_since: float | None = None
        self._last: tuple[float, float] | None = None
        self._samples: deque[bool] = deque(maxlen=WINDOW)
        self.last_verdict: dict | None = None

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="xq-probe").start()

    def _run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 — never let the probe die
                print(f"xq-probe: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(1.0 if self._watching() else 5.0)

    def _watching(self) -> bool:
        return self._key is not None and self._playing_since is not None

    def _bt(self) -> dict:
        try:
            return json.loads(BT_STATUS_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _reset_samples(self) -> None:
        self._playing_since = None
        self._last = None
        self._samples.clear()

    def tick(self) -> None:
        bt = self._bt()
        mac, codec = bt.get("device_mac") or bt.get("mac"), bt.get("codec")
        key = (mac, codec) if bt.get("connected") else None
        if key != self._key:
            # New connection or new codec: judge afresh.
            self._key = key
            self._xq_judged = codec != "SBC-XQ"
            self._reloaded_at = None
            self._reload_failed_logged = False
            self._healthy_logged = False
            self._reset_samples()
        if key is None or not self.mpv.available():
            return

        now = time.monotonic()
        idle = self.mpv.get_property_safe("core-idle", True)
        paused = self.mpv.get_property_safe("pause", False)
        pos = self.mpv.get_property_safe("time-pos")
        if idle or paused or pos is None:
            self._reset_samples()
            return
        if self._playing_since is None:
            self._playing_since = now
        if now - self._playing_since < SKIP_S:
            self._last = (now, pos)
            return

        path = str(self.mpv.get_property_safe("path", "") or "")
        # ytdl:// too: a web link's path is its playlist entry, not the URL.
        if path.startswith(("http://", "https://", "ytdl://")):
            cache = self.mpv.get_property_safe("demuxer-cache-duration", 0) or 0
            if cache < MIN_CACHE_S:
                self._last = None  # the gap across a starved spell proves nothing
                return

        if self._last is not None:
            wall = now - self._last[0]
            # A late tick or a seek makes the ratio meaningless; skip it.
            if 0.7 <= wall <= 1.6 and pos >= self._last[1]:
                self._samples.append((pos - self._last[1]) / wall < BEHIND)
        self._last = (now, pos)

        if len(self._samples) < WINDOW:
            return
        self._judge(mac, codec, now)

    def _judge(self, mac: str, codec: str, now: float) -> None:
        behind = sum(self._samples)
        of = len(self._samples)
        self.last_verdict = {"mac": mac, "codec": codec, "behind": behind, "of": of, "at": time.time()}

        if behind < GLITCH_BEHIND:
            if not self._xq_judged:
                self._xq_judged = True
                print(f"xq-probe: {mac} keeps up with SBC-XQ ({behind}/{of} behind)", flush=True)
            if self._reloaded_at is not None and not self._healthy_logged:
                # Said once, so the log shows the reload worked.
                self._healthy_logged = True
                print(f"xq-probe: {mac} output healthy again after the reload ({behind}/{of})", flush=True)
            return

        can_reload = self._reloaded_at is None or now - self._reloaded_at >= RELOAD_COOLDOWN_S
        if can_reload:
            print(
                f"xq-probe: {mac} ({codec}) output falling behind ({behind}/{of}) — reloading mpv's audio output",
                flush=True,
            )
            self._reloaded_at = now
            self._reload_failed_logged = False
            self._healthy_logged = False
            try:
                self.mpv.command("ao-reload")
            except Exception as exc:  # noqa: BLE001 — mpv gone or busy: the next window retries
                print(f"xq-probe: ao-reload failed: {type(exc).__name__}: {exc}", flush=True)
            # The reload itself stalls the clock for a moment; judge from scratch.
            self._reset_samples()
            return

        if codec == "SBC-XQ" and not self._xq_judged and behind > MAX_BEHIND:
            self._xq_judged = True
            self._reconnected_at = now  # --deny-xq reconnects the speaker itself
            print(
                f"xq-probe: {mac} falls behind on SBC-XQ ({behind}/{of}) even after an output reload"
                " — moving it to standard SBC",
                flush=True,
            )
            subprocess.run(
                [str(INSTALL_DIR / "scripts" / "bt_codec_select.sh"), "--deny-xq", mac],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=120,
                check=False,
            )
            if self.on_reconnected is not None:
                self.on_reconnected()
            return

        # The key survives a reconnect (same speaker, same codec), so this
        # cooldown holds across it.
        if self._reconnected_at is None or now - self._reconnected_at >= RECONNECT_COOLDOWN_S:
            self._reconnected_at = now
            print(
                f"xq-probe: {mac} ({codec}) still falling behind ({behind}/{of}) after an output reload"
                " — reconnecting the speaker",
                flush=True,
            )
            self._reconnect(mac)
            self._reset_samples()
            return

        if not self._reload_failed_logged:
            self._reload_failed_logged = True
            print(
                f"xq-probe: {mac} ({codec}) still falling behind ({behind}/{of}) after a reload and a reconnect;"
                " leaving it alone for now",
                flush=True,
            )
        # Keep sliding; don't repeat the verdict every second.
        self._samples.clear()

    def _reconnect(self, mac: str) -> None:
        try:
            subprocess.run(["bluetoothctl", "disconnect", mac], capture_output=True, text=True, errors="replace", timeout=20, check=False)
            time.sleep(3)
            subprocess.run(
                [str(INSTALL_DIR / "scripts" / "bt_connect.sh"), mac],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"xq-probe: reconnect failed: {type(exc).__name__}: {exc}", flush=True)
        if self.on_reconnected is not None:
            self.on_reconnected()

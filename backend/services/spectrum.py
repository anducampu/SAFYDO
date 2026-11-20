"""Stereo VU tap — one astats branch inside mpv's af chain.

The audio path stays single-ao (a second ALSA output broke bluealsa); the tap
is an asplit side branch measured with astats (per-channel RMS) and written by
ametadata to two tmpfs logs (left / right). This replaces the old 5-band
spectrum tap with a lighter, faster stereo level meter — one measurement pass
instead of five bandpass branches.

The graph keeps the "viztap" label so player._viz_filters_present() still
recognises an attached tap and does not try to re-add it.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.services.mpv import MpvClient

VIZ_DIR = Path("/run/audio-player")
LEFT_LOG = VIZ_DIR / "viz_left.log"
RIGHT_LOG = VIZ_DIR / "viz_right.log"
CHANNEL_LOGS = (LEFT_LOG, RIGHT_LOG)
RMS_RE = re.compile(r"RMS_level=(-?[\d.]+)")

# Tap runs at 24 kHz; 1200 samples = one 50 ms stats frame (20 fps).
TAP_RATE = 24000
TAP_FRAME = 1200

# VU scale: unlike the old per-band auto-gain spectrum, a VU meter shows real
# level — quiet is short, loud is tall — so map a fixed RMS window to the bar.
FLOOR_DB = -50.0
SPAN_DB = 0.0 - FLOOR_DB

TAIL_BYTES = 2048
# ametadata appends forever (~1.5 kB/s per channel); sparse-truncate now and
# then so tmpfs pages free while mpv keeps writing at its own offset. Measured
# as bytes written *since the last rotation*, never as raw st_size — see
# _read_last_rms for why that distinction is the whole ballgame.
ROTATE_BYTES = 4 * 1024 * 1024

# Fast meter: near-instant attack, quick release so the line tracks transients.
ATTACK = 0.9
RELEASE = 0.28
DECAY = 0.82

# The tap sits in mpv's filter chain, which runs ~one output buffer *ahead* of
# what the speaker plays: mpv --audio-buffer=0.5 s + BlueALSA/A2DP + speaker
# buffer (~0.15 s). So the measured level leads the sound by ~0.65 s. We hold a
# short history and emit the level from OUTPUT_DELAY_S ago, so the bars line up
# with what's actually heard. Env-tunable if a speaker's latency differs.
OUTPUT_DELAY_S = float(os.environ.get("VU_DELAY_S", "0.65"))
# Cover the delay plus slack at the ~20 Hz tick rate.
HISTORY_LEN = max(16, round((OUTPUT_DELAY_S + 2.0) * 25))


class SpectrumTap:
    """Reads per-channel astats logs; exposes normalized 0..1 [left, right]."""

    def __init__(self, mpv: MpvClient | None = None) -> None:
        self._mpv = mpv
        self._smooth = [0.0, 0.0]
        self._last_size = [0, 0]
        self._stale_ticks = [0, 0]
        # Apparent size each log was last sparse-truncated at; bytes written
        # past it is what actually occupies tmpfs.
        self._rotate_base = [0, 0]
        self._attached = False
        self._lock = threading.Lock()
        # (monotonic_ts, [L, R]) samples, for the output-latency delay.
        self._history: deque[tuple[float, list[float]]] = deque(maxlen=HISTORY_LEN)

    # Filters ahead of the split, for a source that needs them. See RAW_PCM_GROUP
    # in backend.services.radio.
    pre_filter = ""

    def bind_mpv(self, mpv: MpvClient) -> None:
        self._mpv = mpv

    def filter_specs(self) -> list[str]:
        # astats sets lavfi.astats.<ch>.RMS_level per channel each frame; two
        # ametadata prints fan channel 1/2 out to their own tmpfs logs. A mono
        # source only sets channel 1, so the right log stays empty → mirrored.
        graph = (
            f"{self.pre_filter}asplit=2[vizmain][viztap];"
            f"[viztap]aresample={TAP_RATE},asetnsamples=n={TAP_FRAME},"
            "astats=metadata=1:reset=1:measure_overall=none:measure_perchannel=RMS_level,"
            f"ametadata=mode=print:key=lavfi.astats.1.RMS_level:file={LEFT_LOG}:direct=1,"
            f"ametadata=mode=print:key=lavfi.astats.2.RMS_level:file={RIGHT_LOG}:direct=1,"
            "anullsink;"
            "[vizmain]anull"
        )
        return [f"lavfi=[{graph}]"]

    def prepare_log(self) -> None:
        """Truncate channel logs before mpv (re)opens ametadata — never after attach."""
        try:
            VIZ_DIR.mkdir(parents=True, exist_ok=True)
            for log in CHANNEL_LOGS:
                log.write_text("")
        except OSError:
            pass
        with self._lock:
            # A fresh ametadata opens the log O_TRUNC, so its write offset is
            # back at zero and so is our rotation baseline.
            self._rotate_base = [0, 0]
            self._last_size = [0, 0]
            self._stale_ticks = [0, 0]

    def set_attached(self, attached: bool) -> None:
        self._attached = attached

    def tick(self, *, analyze: bool) -> list[float]:
        with self._lock:
            if analyze:
                dbs = [self._read_last_rms(i, log) for i, log in enumerate(CHANNEL_LOGS)]
                # Mono source: only channel 1 is measured — mirror L onto R.
                if dbs[1] is None and dbs[0] is not None:
                    dbs[1] = dbs[0]
                for i, db in enumerate(dbs):
                    if db is None:
                        self._smooth[i] *= DECAY
                        continue
                    db = max(-90.0, min(0.0, db))
                    target = max(0.0, min(1.0, (db - FLOOR_DB) / SPAN_DB))
                    alpha = ATTACK if target > self._smooth[i] else RELEASE
                    self._smooth[i] += (target - self._smooth[i]) * alpha
            else:
                self._decay_all()

            now = time.monotonic()
            self._history.append((now, list(self._smooth)))
            return self._delayed(now)

    def levels(self) -> list[float]:
        with self._lock:
            return self._delayed()

    def _delayed(self, now: float | None = None) -> list[float]:
        """The level from OUTPUT_DELAY_S ago — aligned with what's heard."""
        if not self._history:
            return self._out()
        cutoff = (now if now is not None else time.monotonic()) - OUTPUT_DELAY_S
        chosen = self._history[0][1]
        for ts, val in self._history:
            if ts <= cutoff:
                chosen = val
            else:
                break
        return [round(v, 4) for v in chosen]

    def _out(self) -> list[float]:
        return [round(v, 4) for v in self._smooth]

    def _read_last_rms(self, i: int, log: Path) -> float | None:
        try:
            size = log.stat().st_size
            if size < self._rotate_base[i]:
                # Smaller than where we last rotated: mpv reopened the log, so
                # its write offset — and our baseline — are back at zero.
                self._rotate_base[i] = 0
            if size < 16:
                return None
            if size - self._rotate_base[i] > ROTATE_BYTES:
                # Sparse-truncate frees the tmpfs pages, but mpv holds the log
                # open at its own offset: the next write lands back out at ~size
                # over a hole, so st_size never comes down. Hence the baseline
                # moves with it. Comparing raw st_size instead re-truncated on
                # every tick from here on and never returned a level again —
                # both VU meters died ~45 min into a stream and only came back
                # when the filter chain was rebuilt (play/change stream).
                os.truncate(log, 0)
                self._rotate_base[i] = size
                self._last_size[i] = 0
                self._stale_ticks[i] = 0
                return None
            # Freshness: when playback stalls mpv stops writing, so the last
            # value would pin the meter forever. Tolerate a few static reads.
            if size == self._last_size[i]:
                self._stale_ticks[i] += 1
                if self._stale_ticks[i] > 6:
                    return None
            else:
                self._last_size[i] = size
                self._stale_ticks[i] = 0
            with log.open("r", errors="replace") as fh:
                fh.seek(max(0, size - TAIL_BYTES))
                chunk = fh.read()
        except OSError:
            return None
        values = RMS_RE.findall(chunk)
        if not values:
            return None  # silence prints -inf, which the regex skips
        return float(values[-1])

    def _decay_all(self) -> None:
        for i in range(len(self._smooth)):
            self._smooth[i] *= DECAY

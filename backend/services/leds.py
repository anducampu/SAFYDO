"""Blinkt! — 8× APA102 RGB LEDs driven as a physical stereo VU meter.

Blinkt is bit-banged APA102 on BCM GPIO 23 (data) / 24 (clock). We drive it
with lgpio (already on Bookworm; RPi.GPIO is flaky there) — no extra pip deps.
Everything is guarded so importing/using this off-Pi (no lgpio / no chip / pins
busy) is a safe no-op with ``available = False``.

Layout mirrors the on-screen stereo VU: a centre-out meter. The left channel
fills LEDs 3→0 and the right fills 4→7, green (centre) through amber to red
(ends), scaled by a global brightness.

GPIO 23 is also the DABBoard tuner's RSTB, and an APA102 frame is just toggling
on that pin. So nothing here touches the header until the LEDs are actually
switched on (the backend builds the driver only then), and the driver refuses
outright when the tuner's overlay is configured. A kernel line claim would not
protect the tuner in either direction: radio_cli drives the pin through
wiringPi, not through gpiod.
"""

from __future__ import annotations

NUM = 8
DAT = 23  # BCM — Blinkt data
CLK = 24  # BCM — Blinkt clock

CONFIG_TXT = ("/boot/firmware/config.txt", "/boot/config.txt")
RADIO_BLOCK_MARKER = "# BEGIN SAFYDO RADIO"

try:
    import lgpio  # type: ignore

    _HAVE_LGPIO = True
except Exception:  # pragma: no cover - off-Pi
    _HAVE_LGPIO = False


def radio_overlay_configured(paths: tuple[str, ...] = CONFIG_TXT) -> bool:
    """True when config.txt sets up the tuner, whose reset line is our data line.

    A file we can see but not read counts as configured: the cost of being wrong
    is a dark LED strip one way and clocking a powered tuner's reset the other.
    """
    for path in paths:
        try:
            with open(path) as fh:
                lines = fh.read().splitlines()
        except FileNotFoundError:
            continue
        except OSError:
            return True
        for line in lines:
            text = line.strip()
            if text == RADIO_BLOCK_MARKER or text.startswith("dtoverlay=ugreen-dabboard"):
                return True
    return False


def _gpiochip_candidates() -> list[int]:
    """gpiochip numbers to try, the header's own controller first.

    Chip 0 is the 40-pin header on every Pi except the 5 (RP1 was gpiochip4 on
    Bookworm, gpiochip0 again on Trixie), and kernel updates have reshuffled the
    numbering before. Resolve by sysfs label and keep 0 as the fallback so this
    holds across boards and kernels without hardcoding one answer.
    """
    import glob
    import os

    wanted = ("pinctrl-bcm2835", "pinctrl-bcm2711", "pinctrl-rp1", "gpio-brcmstb")
    found: list[int] = []
    for path in sorted(glob.glob("/sys/class/gpio/gpiochip*/label")) + sorted(
        glob.glob("/sys/bus/gpio/devices/gpiochip*/label")
    ):
        try:
            with open(path) as fh:
                label = fh.read().strip()
        except OSError:
            continue
        if label in wanted:
            name = os.path.basename(os.path.dirname(path))  # gpiochipN
            digits = name[len("gpiochip"):]
            if digits.isdigit() and int(digits) not in found:
                found.append(int(digits))
    if 0 not in found:
        found.append(0)
    return found


def driver_usable(driver: "BlinktLeds | None") -> bool:
    """Whether to offer the switch. Answers without opening the GPIO chip."""
    if driver is not None:
        return driver.available
    return _HAVE_LGPIO and not radio_overlay_configured()


# Per-position colour, centre (quiet) → end (loud).
_COLORS = [(0, 210, 45), (150, 220, 0), (255, 140, 0), (255, 25, 0)]
_LEFT = [3, 2, 1, 0]   # left channel, centre outward
_RIGHT = [4, 5, 6, 7]  # right channel, centre outward


class BlinktLeds:
    def __init__(self) -> None:
        self._h = None
        self.chip = None
        self.available = False
        self.error = "" if _HAVE_LGPIO else "lgpio not importable"
        if not _HAVE_LGPIO:
            return
        if radio_overlay_configured():
            self.error = "GPIO 23 belongs to the DABBoard tuner (overlay in config.txt)"
            return
        for chip in _gpiochip_candidates():
            try:
                self._h = lgpio.gpiochip_open(chip)
                lgpio.gpio_claim_output(self._h, DAT, 0)
                lgpio.gpio_claim_output(self._h, CLK, 0)
                self.chip = chip
                self.available = True
                self.error = ""
                self.clear()
                return
            except Exception as exc:
                self.error = f"gpiochip{chip}: {type(exc).__name__}: {exc}"
                if self._h is not None:
                    try:
                        lgpio.gpiochip_close(self._h)
                    except Exception:
                        pass
                self._h = None
                self.available = False

    def _byte(self, b: int) -> None:
        h = self._h
        for i in range(8):
            lgpio.gpio_write(h, DAT, (b >> (7 - i)) & 1)
            lgpio.gpio_write(h, CLK, 1)
            lgpio.gpio_write(h, CLK, 0)

    def _show(self, pixels: list[tuple[int, int, int, int]]) -> None:
        for _ in range(4):  # start frame
            self._byte(0x00)
        for r, g, b, bright in pixels:
            self._byte(0xE0 | (bright & 0x1F))  # 111 + 5-bit global brightness
            self._byte(b)  # APA102 order: blue, green, red
            self._byte(g)
            self._byte(r)
        for _ in range(((NUM + 15) // 16) + 1):  # end frame ≥ n/2 clocks
            self._byte(0x00)

    def render(self, levels: list[float], brightness: float) -> None:
        if not self.available:
            return
        left = max(0.0, min(1.0, levels[0] if levels else 0.0))
        right = max(0.0, min(1.0, levels[1] if len(levels) > 1 else left))
        # APA102 dims two ways: a coarse 5-bit global (1..31) plus 8-bit RGB PWM.
        # Use the global for the top of the range and, once it bottoms out at 1
        # (~3%), keep dimming by scaling the RGB — so 1% is genuinely faint.
        gb = max(1, min(31, round(brightness * 31)))
        rgb_scale = min(1.0, brightness * 31)
        px: list[tuple[int, int, int, int]] = [(0, 0, 0, gb)] * NUM
        for chan, order in ((left, _LEFT), (right, _RIGHT)):
            lit = chan * 4.0
            for pos, led in enumerate(order):
                if pos + 1 <= lit:
                    frac = 1.0
                elif pos < lit:
                    frac = lit - pos
                else:
                    continue
                r, g, b = _COLORS[pos]
                s = frac * rgb_scale
                px[led] = (round(r * s), round(g * s), round(b * s), gb)
        try:
            self._show(px)
        except Exception:
            self.available = False

    def clear(self) -> None:
        if not self.available:
            return
        try:
            self._show([(0, 0, 0, 0)] * NUM)
        except Exception:
            self.available = False

    def close(self) -> None:
        try:
            if self._h is not None:
                self.clear()
                lgpio.gpiochip_close(self._h)
        except Exception:
            pass
        self._h = None
        self.available = False

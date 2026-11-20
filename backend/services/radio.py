"""DABBoard FM receiver: drives radio_cli and feeds its I2S audio to mpv.

Control is the vendor's closed radio_cli, a one-shot process that leaves the
chip running. Audio takes arecord -> FIFO -> mpv, deliberately not mpv's own
av://alsa: ffmpeg's ALSA reader polls a non-blocking handle, which pinned a core
at 100 % and took the Bluetooth controller down within a minute on the 3 A+. The
same audio through a blocking arecord costs ~10 % of a core. See
docs/dabboard-plan.md.

The chip is exposed to damage from a power cut while out of reset, so it runs
only while someone is listening: every other source, Stop, and a speaker that
stays away all shut it down. A worker thread owns the slow work, so nothing on
the 1 Hz status path ever runs radio_cli.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from backend.services.leds import radio_overlay_configured
from backend.services.mpv import MpvError
from backend.services.rds import RdsDecoder
from backend.textsafe import display

RADIO_DIR = Path(os.environ.get("SAFYDO_RADIO_DIR", "/usr/local/lib/ugreen-dab+"))
RADIO_CLI = RADIO_DIR / "radio_cli"
FIFO = "/run/audio-player/radio.fifo"
CAPTURE_UNIT = "audio-player-radio-capture.service"
WIRINGPI_LIBS = ("/usr/lib/libwiringPi.so", "/usr/local/lib/libwiringPi.so", "/lib/libwiringPi.so")

FM_MIN_KHZ = 87500
FM_MAX_KHZ = 108000
FM_STEP_KHZ = 100
# radio_cli's top level. mpv's slider does the attenuating, and a second gain
# stage in front of the capture would only cost headroom.
CHIP_LEVEL = "63"

# Each radio_cli -r returns one RDS group from the chip's queue. Stations send
# about 11 a second and repeat themselves, so sampling is enough: quicker just
# after tuning, when everything is still unknown, then slow.
RDS_POLL_FAST_S = 0.25
RDS_POLL_SLOW_S = 1.0
RDS_FAST_FOR_S = 90.0
FROZEN_AFTER_S = 10.0
RETRY_EVERY_S = 15.0
RETRY_LIMIT = 4
# Raw PCM reaches the filter chain in tiny frames, and the VU branch then handles
# only ~40 % of them. The rest queue up inside the graph at about 3.5 MB/min for
# as long as a web page is open. Grouping them ahead of the split (50 ms here)
# keeps the branch at full rate with memory flat.
RAW_PCM_GROUP = "asetnsamples=n=2400,"
# Mono for a weak station. FM's noise sits mainly in the stereo difference signal,
# so summing L and R cancels it. Done here because radio_cli has no way to set the
# chip's own stereo controls. Keeps two channels: the Bluetooth link stays stereo.
MONO_FILTER = "lavfi=[pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1]"
# How long the chip stays up waiting for a speaker that went away.
NO_SPEAKER_GRACE_S = 300.0
WAITING_FOR_SPEAKER = "Waiting for the speaker"

_FIELDS = {
    "frequency_khz": r"Currently tuned frequency:\s*(-?\d+)",
    "rssi": r"RSSI:\s*(-?\d+)",
    "snr": r"Signal Noise Ratio:\s*(-?\d+)",
    "valid": r"Valid channel:\s*(\d+)",
    "multipath": r"Multipath indicator:\s*(\d+)",
    "rds_sync": r"RDS synchronized:\s*(\d+)",
}


def parse_rsq(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, pattern in _FIELDS.items():
        match = re.search(pattern, text)
        if match:
            out[key] = int(match.group(1))
    return out


def parse_rds_group(text: str) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]] | None:
    """The raw group and its four error grades from `radio_cli -r`, if one came back."""
    blocks = re.findall(r"^Block ([ABCD])\s+(\d+)\s*$", text, re.M)
    errors = re.findall(r"Bit Errors in Block ([ABCD]):\s*(\d+)", text)
    if len(blocks) != 4 or len(errors) != 4:
        return None
    values = {k: int(v) for k, v in blocks}
    grades = {k: int(v) for k, v in errors}
    return (
        (values["A"], values["B"], values["C"], values["D"]),
        (grades["A"], grades["B"], grades["C"], grades["D"]),
    )


def valid_khz(value: Any) -> int:
    try:
        khz = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("frequency must be a whole number of kHz") from exc
    if not FM_MIN_KHZ <= khz <= FM_MAX_KHZ:
        raise ValueError(f"frequency must be between {FM_MIN_KHZ} and {FM_MAX_KHZ} kHz")
    return khz


class RadioService:
    def __init__(self, player, install_dir: Path, state_dir: Path, cfg_get, cfg_set) -> None:
        self.player = player
        self.mpv = player.mpv
        self.install_dir = install_dir
        self._cfg_get = cfg_get
        self._cfg_set = cfg_set
        self.presets_file = state_dir / "radio_presets.json"
        self.stations_file = state_dir / "radio_stations.json"
        self.mono_file = state_dir / "radio_mono.json"
        self._lock = threading.RLock()  # one radio_cli at a time, always
        self._stop_worker = threading.Event()
        self._worker: threading.Thread | None = None
        self.listening = False
        self.frequency_khz = 0
        self.signal: dict[str, int] = {}
        self.rds = RdsDecoder(self._region())
        self._tuned_at = 0.0
        self.not_detected = False
        self.last_error = ""
        self.scan: dict[str, Any] = {"running": False, "progress": 0, "found": 0}
        self._chip_up = False
        self._last_pos: float | None = None
        self._pos_moved_at = 0.0
        self._last_retry = 0.0
        self._retries = 0
        self._no_speaker_since = 0.0
        self._resume_pending = False
        self._last_resume_try = 0.0
        # Seek and scan each sweep the band; only one at a time.
        self._sweep = threading.Lock()

    def _region(self) -> str:
        """E: RDS as used in Europe. U: RBDS as used in North America."""
        return "U" if str(self._cfg_get("radio.rds_region") or "E").upper() == "U" else "E"

    def set_region(self, region: str) -> None:
        if region not in ("E", "U"):
            raise ValueError("region must be E (RDS) or U (RBDS)")
        self._cfg_set("radio.rds_region", region)
        self.rds.region = region

    # --- radio_cli ---------------------------------------------------------

    def _cli(self, *args: str, timeout: float = 60.0) -> str:
        with self._lock:
            proc = subprocess.run(
                [str(RADIO_CLI), *args],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
                cwd="/tmp",  # -u drops its scan file in the working directory
            )
        return (proc.stdout or "") + (proc.stderr or "")

    def _shutdown_chip(self) -> None:
        """Into reset. If that can't be confirmed _chip_up stays set, and the worker
        tries again: a chip left out of reset is exposed to a power cut."""
        if not RADIO_CLI.is_file():
            self._chip_up = False
            return
        try:
            if "shut down" in self._cli("-k", timeout=20):
                self._chip_up = False
        except (OSError, subprocess.SubprocessError):
            pass

    def _boot_and_tune(self, khz: int) -> None:
        # Here and not only behind Listen: the worker boots the chip too.
        if self._undervoltage_now():
            raise MpvError("Power supply is under voltage; not starting the tuner")
        # Set first: a timeout part way through can leave the chip out of reset.
        self._chip_up = True
        try:
            out = self._cli(
                "-b", "F", "-o", "1", "-F", str(khz), "-l", CHIP_LEVEL, "-R", self._region()
            )
        except (OSError, subprocess.SubprocessError):
            self._shutdown_chip()
            raise
        if "Tuned" not in out:
            self.not_detected = True
            self._shutdown_chip()
            raise MpvError("The DABBoard did not answer. Check that it is seated on the header")
        self.not_detected = False

    def _chip_running(self) -> bool:
        try:
            out = self._cli("-r", timeout=15)
        except (OSError, subprocess.SubprocessError):
            return False
        return "not booted" not in out and "Currently tuned frequency" in out

    # --- preconditions -----------------------------------------------------

    @staticmethod
    def board_supported() -> bool:
        forced = os.environ.get("SAFYDO_BOARD")
        if forced:
            return forced == "pi3aplus"
        try:
            model = Path("/proc/device-tree/model").read_text(errors="replace")
        except OSError:
            return False
        return "Pi 3 Model A Plus" in model

    def enabled(self) -> bool:
        return str(self._cfg_get("radio.enabled") or "false").lower() in ("true", "1", "yes")

    @staticmethod
    def _card_present() -> bool:
        try:
            return "dabboard" in Path("/proc/asound/cards").read_text()
        except OSError:
            return False

    @staticmethod
    def _tools_present() -> bool:
        return RADIO_CLI.is_file() and any(Path(p).exists() for p in WIRINGPI_LIBS)

    def state(self) -> str:
        """Derived on every read, never stored, so it cannot drift from the board."""
        if not self.board_supported():
            return "unsupported"
        if not self.enabled():
            return "disabled"
        if not self._tools_present():
            return "setup_needed"
        if not (
            radio_overlay_configured() and self._card_present() and Path("/dev/spidev0.0").exists()
        ):
            return "reboot_required"
        if self.not_detected:
            return "not_detected"
        return "playing" if self.listening else "ready"

    @staticmethod
    def _undervoltage_now() -> bool:
        try:
            out = subprocess.run(
                ["vcgencmd", "get_throttled"], capture_output=True, text=True, errors="replace", timeout=5, check=False
            ).stdout
            return bool(int(out.strip().split("=")[1], 16) & 0x1)
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return False

    @staticmethod
    def _speaker_ready() -> bool:
        try:
            out = subprocess.run(
                ["bluealsa-cli", "list-pcms"], capture_output=True, text=True, errors="replace", timeout=5, check=False
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return "a2dpsrc/sink" in out

    # --- capture -----------------------------------------------------------

    @staticmethod
    def _systemctl(*args: str) -> None:
        # Never raises: stopping capture must not get in the way of the chip's reset.
        try:
            subprocess.run(["systemctl", *args], capture_output=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            pass

    def _set_mpv_options(self, on: bool) -> None:
        self.player.spectrum.pre_filter = RAW_PCM_GROUP if on else ""
        # Global, because loadfile's options argument differs between the mpv on
        # Bookworm and on Trixie. The cache limits matter: live audio has nothing
        # to seek back into, and with the default donation the back-buffer takes
        # all 50 MiB. Raw PCM arrives in tiny packets that cost about three times
        # their size, which is over 150 MB on a 472 MB board.
        values = (
            {
                "options/demuxer": "rawaudio",
                "options/demuxer-rawaudio-format": "s16le",
                "options/demuxer-rawaudio-rate": 48000,
                "options/demuxer-rawaudio-channels": "stereo",
                "options/demuxer-max-bytes": "2M",
                "options/demuxer-max-back-bytes": "256K",
                "options/demuxer-donate-buffer": "no",
                # A looped link playlist's "inf" is global: the FIFO would be
                # reopened (and block) every time the capture ends.
                "loop-playlist": "no",
            }
            if on
            else {
                "options/demuxer": "",  # the default; "auto" is not a valid value
                "options/demuxer-max-bytes": "50M",
                "options/demuxer-max-back-bytes": "50MiB",
                "options/demuxer-donate-buffer": "yes",
                "force-media-title": "",
                "mute": False,
            }
        )
        for name, value in values.items():
            self.mpv.set_property(name, value)

    def _start_audio(self) -> None:
        self._systemctl("reset-failed", CAPTURE_UNIT)
        self._systemctl("start", CAPTURE_UNIT)
        self._set_mpv_options(True)
        self.mpv.load(FIFO, "replace")
        self.mpv.pause(False)
        self._set_title()
        self._last_pos = None
        self._pos_moved_at = time.monotonic()
        self.player.refresh_audio_filters()

    def _stop_audio(self) -> None:
        self._systemctl("stop", CAPTURE_UNIT)

    def _set_title(self) -> None:
        try:
            self.mpv.set_property("force-media-title", self.display_name())
        except (MpvError, OSError):
            pass

    def display_name(self) -> str:
        for preset in self.presets():
            if preset.get("frequency_khz") == self.frequency_khz:
                return str(preset.get("name") or "")
        # RDS is whatever the transmitter sends: shown through display().
        return display(self.rds.ps, 64) or f"{self.frequency_khz / 1000:.1f} FM"

    # --- listening ---------------------------------------------------------

    def _remember(self, khz: int) -> None:
        self.frequency_khz = khz
        self.last_error = ""
        self.rds.reset()
        self._tuned_at = time.monotonic()
        self.signal = {}
        self.player._write_state(
            source="radio",
            frequency_khz=khz,
            url=FIFO,
            meta={"name": self.display_name(), "genre": "FM"},
        )
        self._cfg_set("radio.last_frequency_khz", str(khz))
        self._apply_mono()

    # --- mono ---------------------------------------------------------------

    def _mono_set(self) -> set[int]:
        try:
            data = json.loads(self.mono_file.read_text())
        except (OSError, json.JSONDecodeError):
            return set()
        return {x for x in data if isinstance(x, int)} if isinstance(data, list) else set()

    @property
    def mono(self) -> bool:
        """Remembered per frequency: it's the weak stations that need it."""
        return (self.frequency_khz or self._last_khz()) in self._mono_set()

    def set_mono(self, enabled: bool) -> dict:
        khz = self.frequency_khz or self._last_khz()
        chosen = self._mono_set()
        chosen.discard(khz)
        if enabled:
            chosen.add(khz)
        self.mono_file.write_text(json.dumps(sorted(chosen)) + "\n")
        self._apply_mono()
        return self.status()

    def _apply_mono(self) -> None:
        wanted = [MONO_FILTER] if self.listening and self.mono else []
        if self.player.eq.pre_filters != wanted:
            self.player.eq.pre_filters = wanted
            if self.listening:
                self.player.refresh_audio_filters()

    def _last_khz(self) -> int:
        try:
            return valid_khz(self._cfg_get("radio.last_frequency_khz"))
        except ValueError:
            return FM_MIN_KHZ

    def play(self, frequency_khz: int | None = None) -> dict:
        from backend.services.audio_verify import ensure_playback_ready

        khz = valid_khz(frequency_khz) if frequency_khz else self._last_khz()
        state = self.state()
        if state not in ("ready", "playing", "not_detected"):
            raise ValueError(f"radio is not available: {state}")
        self._resume_pending = False
        with self._lock:
            ensure_playback_ready(self.mpv, self._cfg_get, self.install_dir)
            self._start_listening(khz)
        return self.status()

    def _start_listening(self, khz: int) -> None:
        """The ordered start: capture closed, chip running and tuned, capture open.

        A running chip keeps its firmware and is only retuned: uploading firmware
        to it again would stop the I2S clock for no reason.
        """
        self._stop_audio()
        if self._chip_running():
            self._cli("-F", str(khz))
            self._chip_up = True
        else:
            self._boot_and_tune(khz)
        self.listening = True
        self.last_error = ""
        self._remember(khz)
        self._start_audio()

    def tune(self, frequency_khz: int) -> dict:
        khz = valid_khz(frequency_khz)
        if not self.listening:
            return self.play(khz)
        self._require_chip()
        with self._lock:
            if "Tuned" not in self._cli("-F", str(khz)):
                raise MpvError("Tuner did not accept the frequency")
            self._remember(khz)
            self._set_title()
        return self.status()

    def _require_chip(self) -> None:
        if not self.listening:
            raise ValueError("Start the radio first")
        if not self._chip_up:
            raise ValueError("Waiting for the speaker: the tuner restarts when it returns")

    def seek(self, up: bool = True) -> dict:
        """Next or previous station that reads as a valid channel, wrapping once."""
        self._require_chip()
        if not self._sweep.acquire(blocking=False):
            raise ValueError("Already searching")
        step = FM_STEP_KHZ if up else -FM_STEP_KHZ
        span = FM_MAX_KHZ - FM_MIN_KHZ + FM_STEP_KHZ
        start = self.frequency_khz or self._last_khz()
        found = False
        self._mute(True)
        try:
            for n in range(1, span // FM_STEP_KHZ + 1):
                khz = FM_MIN_KHZ + ((start + step * n) - FM_MIN_KHZ) % span
                # Lock per step, and stop as soon as nobody is listening: a sweep of
                # a weak band takes minutes, and Stop must not wait for it.
                with self._lock:
                    if not self.listening:
                        break
                    self._cli("-F", str(khz))
                    if self._is_station(parse_rsq(self._cli("-r"))):
                        self._remember(khz)
                        self._set_title()
                        found = True
                        break
        finally:
            with self._lock:
                if self.listening and not found:
                    self._retune_quietly(start)
            self._mute(False)
            self._sweep.release()
        if found:
            return self.status()
        if not self.listening:
            return self.status()
        raise ValueError("No station found")

    def _retune_quietly(self, khz: int) -> None:
        try:
            self._cli("-F", str(khz))
        except (OSError, subprocess.SubprocessError):
            pass

    @staticmethod
    def _is_station(rsq: dict[str, int]) -> bool:
        return bool(rsq.get("valid")) and rsq.get("snr", 0) >= 6

    def _mute(self, on: bool) -> None:
        try:
            self.mpv.set_property("mute", on)
        except (MpvError, OSError):
            pass

    def stop(self) -> dict:
        self.release()
        try:
            self.mpv.stop()
        except (MpvError, OSError):
            pass
        return self.status()

    def release(self) -> None:
        """Another source is taking over, or Stop: nothing keeps the chip up."""
        # Ahead of the lock: a sweep takes it again after every step, and would win
        # that race for minutes. This way its next step sees the flag and ends.
        was_listening = self.listening
        self.listening = False
        self._resume_pending = False
        with self._lock:
            self._retries = 0
            self.signal = {}
            self.rds.reset()
            self._no_speaker_since = 0.0
            if not was_listening and not self._chip_up:
                return
            try:
                self._stop_audio()
            finally:
                self._shutdown_chip()
            self.player.eq.pre_filters = []
            try:
                self._set_mpv_options(False)
                # Now, not at the next load: a stream that fails its checks would
                # otherwise keep the radio's filters.
                self.player.refresh_audio_filters()
            except (MpvError, OSError):
                pass

    # --- station scan ------------------------------------------------------

    def start_scan(self) -> dict:
        self._require_chip()
        if not self._sweep.acquire(blocking=False):
            return self.status()
        self.last_error = ""
        self.scan = {"running": True, "progress": 0, "found": 0}
        threading.Thread(target=self._scan, daemon=True, name="radio-scan").start()
        return self.status()

    def _scan(self) -> None:
        back_to = self.frequency_khz
        found: list[dict] = []
        total = (FM_MAX_KHZ - FM_MIN_KHZ) // FM_STEP_KHZ + 1
        self._mute(True)
        try:
            for n in range(total):
                if not self.listening or not self._chip_up:
                    return
                khz = FM_MIN_KHZ + n * FM_STEP_KHZ
                # Lock per step, so a Stop during the scan waits under a second.
                with self._lock:
                    self._cli("-F", str(khz))
                    rsq = parse_rsq(self._cli("-r"))
                if self._is_station(rsq):
                    found.append(
                        {"frequency_khz": khz, "rssi": rsq.get("rssi", 0), "snr": rsq.get("snr", 0)}
                    )
                self.scan = {"running": True, "progress": round(100 * (n + 1) / total), "found": len(found)}
            if found:
                self.stations_file.write_text(json.dumps(found, indent=2) + "\n")
            else:
                # An unplugged antenna looks just like this. Keep what's there.
                self.last_error = "The scan found no stations; the saved list is unchanged"
        except (OSError, subprocess.SubprocessError) as exc:
            self.last_error = f"Scan failed: {exc}"
        finally:
            with self._lock:
                if self.listening and self._chip_up and back_to:
                    self._retune_quietly(back_to)
            self._mute(False)
            self.scan = {"running": False, "progress": 100, "found": len(found)}
            self._sweep.release()

    def stations(self) -> list[dict]:
        return self._read_list(self.stations_file)

    # --- presets -----------------------------------------------------------

    @staticmethod
    def _read_list(path: Path) -> list[dict]:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []

    def presets(self) -> list[dict]:
        return self._read_list(self.presets_file)

    def add_preset(self, name: str, frequency_khz: int) -> dict:
        khz = valid_khz(frequency_khz)
        label = name.strip()[:40] or f"{khz / 1000:.1f} FM"
        kept = [p for p in self.presets() if p.get("frequency_khz") != khz]
        kept.append({"name": label, "frequency_khz": khz})
        kept.sort(key=lambda p: p["frequency_khz"])
        self.presets_file.write_text(json.dumps(kept, indent=2) + "\n")
        self._set_title_if_listening()
        return self.status()

    def delete_preset(self, frequency_khz: int) -> dict:
        kept = [p for p in self.presets() if p.get("frequency_khz") != frequency_khz]
        self.presets_file.write_text(json.dumps(kept, indent=2) + "\n")
        self._set_title_if_listening()
        return self.status()

    def _set_title_if_listening(self) -> None:
        if self.listening:
            self._set_title()

    # --- settings ----------------------------------------------------------

    def set_enabled(self, on: bool) -> None:
        """Marks the option on or off and edits config.txt. Takes effect at reboot."""
        if not self.board_supported():
            raise ValueError("The DABBoard option is only available on the Pi 3 A+")
        if not on:
            # Stop, not just release, or mpv stays parked on the FIFO. Only when
            # listening, though: this mustn't end an online stream that's playing.
            if self.listening:
                self.stop()
            else:
                self.release()
            self.not_detected = False
        script = self.install_dir / "scripts" / "radio_overlay.sh"
        proc = subprocess.run(
            [str(script), "enable" if on else "disable"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            raise OSError(proc.stderr.strip() or "could not update config.txt")
        self._cfg_set("radio.enabled", "true" if on else "false")

    # --- status ------------------------------------------------------------

    def summary(self) -> dict:
        """Cached values only: this runs on the 1 Hz status path."""
        return {
            "state": self.state(),
            "listening": self.listening,
            "frequency_khz": self.frequency_khz or self._last_khz(),
            "name": self.display_name() if self.frequency_khz else "",
            "signal": dict(self.signal),
            "mono": self.mono,
            "radio_text": display(self.rds.rt, 128),
            "rds": self.rds.snapshot() if self.listening else None,
            "error": self.last_error,
        }

    def status(self) -> dict:
        return {
            **self.summary(),
            "presets": self.presets(),
            "stations": self.stations(),
            "scan": dict(self.scan),
            "rds_region": self._region(),
            "band": {"min_khz": FM_MIN_KHZ, "max_khz": FM_MAX_KHZ, "step_khz": FM_STEP_KHZ},
        }

    # --- worker ------------------------------------------------------------

    def start_worker(self) -> None:
        if self._worker is None:
            if not self._adopt_running_session() and self.board_supported() and self.enabled():
                # Nothing is listening, and a chip the last backend left running
                # would otherwise stay out of reset. The worker puts it right.
                self._chip_up = True
            self._worker = threading.Thread(target=self._run, daemon=True, name="radio")
            self._worker.start()
            threading.Thread(target=self._run_rds, daemon=True, name="radio-rds").start()

    def stop_worker(self) -> None:
        self._stop_worker.set()

    def _adopt_running_session(self) -> bool:
        """A backend restart must not interrupt someone listening.

        Only a session that's really playing counts: enabled, capture running and mpv
        on the FIFO. mpv alone stays parked there after capture ended.
        """
        try:
            path = str(self.mpv.get_property_safe("path") or "")
        except (MpvError, OSError):
            return False
        if path != FIFO:
            return False
        if not self.enabled() or not self._capture_active():
            try:
                self.mpv.stop()
            except (MpvError, OSError):
                pass
            return False
        self.listening = True
        self._chip_up = True
        saved = self.player.saved_state().get("frequency_khz")
        self.frequency_khz = saved if isinstance(saved, int) else self._last_khz()
        self._tuned_at = time.monotonic()
        self.player.spectrum.pre_filter = RAW_PCM_GROUP
        self.player.eq.pre_filters = [MONO_FILTER] if self.mono else []
        self._pos_moved_at = time.monotonic()
        # The last backend may have died mid-sweep: muted, on some other frequency.
        self._mute(False)
        try:
            rsq = parse_rsq(self._cli("-r", timeout=15))
            if rsq.get("frequency_khz") not in (None, self.frequency_khz):
                self._retune_quietly(self.frequency_khz)
        except (OSError, subprocess.SubprocessError):
            pass
        return True

    @staticmethod
    def _capture_active() -> bool:
        try:
            return (
                subprocess.run(
                    ["systemctl", "is-active", "--quiet", CAPTURE_UNIT], timeout=10, check=False
                ).returncode
                == 0
            )
        except (OSError, subprocess.SubprocessError):
            return False

    def resume_after_boot(self) -> None:
        """Off by default: an unattended boot should not expose the chip by itself.

        Only marks the wish. The worker acts on it once the speaker is there, however
        long that takes, the way a saved stream resumes when a speaker reconnects.
        """
        flag = str(self._cfg_get("radio.resume_on_boot") or "false").lower()
        if flag in ("true", "1", "yes") and self.player.saved_state().get("source") == "radio":
            self._resume_pending = True

    def resume_interrupted(self) -> bool:
        """Bring back an FM session that a deploy interrupted.

        Not gated on radio.resume_on_boot: someone was listening a moment
        ago, so this is not an unattended boot exposing the chip. The worker
        plays it once the speaker is ready, the same as the boot path.
        """
        if self.player.saved_state().get("source") != "radio":
            return False
        self._resume_pending = True
        return True

    def _try_resume(self, now: float) -> None:
        if now - self._last_resume_try < RETRY_EVERY_S:
            return
        self._last_resume_try = now
        if self.player.saved_state().get("source") != "radio":
            self._resume_pending = False  # another source was chosen meanwhile
            return
        if self.state() == "ready" and self._speaker_ready():
            try:
                self.play(self.player.saved_state().get("frequency_khz"))
            except (ValueError, TypeError):
                self._resume_pending = False
                raise
            self._resume_pending = False

    def _run(self) -> None:
        while not self._stop_worker.wait(1.0):
            now = time.monotonic()
            try:
                if not self.listening:
                    # The rule the power-cut protection rests on: not listening, no
                    # chip. Anything that left it up gets corrected here.
                    with self._lock:
                        if not self.listening and self._chip_up:
                            self._stop_audio()
                            self._shutdown_chip()
                    if self._resume_pending:
                        self._try_resume(now)
                elif not self.scan["running"]:
                    self._check_playback(now)
            except (MpvError, OSError, ValueError, subprocess.SubprocessError) as exc:
                self.last_error = str(exc)

    def _run_rds(self) -> None:
        """Signal and RDS share one call, so this is the only regular radio_cli."""
        delay = RDS_POLL_FAST_S
        while not self._stop_worker.wait(delay):
            if not self.listening or self._sweep.locked() or not self._chip_up:
                delay = 1.0
                continue
            delay = (
                RDS_POLL_FAST_S
                if time.monotonic() - self._tuned_at < RDS_FAST_FOR_S
                else RDS_POLL_SLOW_S
            )
            try:
                self._poll_rds()
            except (OSError, subprocess.SubprocessError):
                pass

    def _poll_rds(self) -> None:
        with self._lock:
            tuned = self.frequency_khz
            text = self._cli("-r", timeout=15)
            # Under the lock, so a retune can't land between reading and feeding.
            rsq = parse_rsq(text)
            if rsq and rsq.get("frequency_khz") != tuned:
                return
            if rsq:
                self.signal = rsq
            group = parse_rds_group(text)
            if group is None:
                return
            before = self.rds.ps
            self.rds.feed(*group[0], group[1])
            if self.rds.ps != before and self.listening:
                self._set_title()

    def _check_playback(self, now: float) -> None:
        """Bring the radio back when mpv lost its output or the capture froze.

        Speaker switched off, link dropped, mpv restarted: all of them end with mpv
        idle or its clock stopped and a chip nobody is hearing. The saved source is
        radio, so the generic resume leaves this alone.
        """
        idle = bool(self.mpv.get_property_safe("idle-active", True))
        pos = self.mpv.get_property_safe("time-pos")
        if not idle and pos is not None and pos != self._last_pos:
            self._last_pos = pos
            self._pos_moved_at = now
            self._retries = 0
            self._no_speaker_since = 0.0
            if self.last_error == WAITING_FOR_SPEAKER:
                self.last_error = ""
            return
        if not idle and now - self._pos_moved_at < FROZEN_AFTER_S:
            return
        if not self._speaker_ready():
            if not self._no_speaker_since:
                self._no_speaker_since = now
                self._stop_audio()
            if self._chip_up and now - self._no_speaker_since > NO_SPEAKER_GRACE_S:
                self._shutdown_chip()
            self.last_error = WAITING_FOR_SPEAKER
            return
        self._no_speaker_since = 0.0
        if now - self._last_retry < RETRY_EVERY_S:
            return
        self._last_retry = now
        if self._retries >= RETRY_LIMIT:
            # Giving up means giving up: not a running chip that nobody hears.
            self.stop()
            self.last_error = "Playback stopped and restarting did not help"
            return
        self._retries += 1
        with self._lock:
            # Stop or another source may have got in while this was deciding.
            if self.listening:
                self._start_listening(self.frequency_khz or self._last_khz())

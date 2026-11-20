"""Record what is playing — an online station, a web link or FM — to a file.

    mpv --stream-record=<fifo.mka>  ->  ffmpeg -f matroska -i <fifo>  ->  file

mpv hands over the packets it already receives (no second download of the
stream), wrapped in Matroska so any codec fits, including the tuner's raw PCM.
ffmpeg encodes live to the chosen format, so a big uncompressed intermediate
never touches the SD card. Writing goes to "<name>.part" and is renamed when
ffmpeg closes the file cleanly; a sidecar "<name>.json" keeps the source and
duration for the Recordings browser.

A monitor thread finalizes the recording when the source changes, mpv
restarts (its stream-record is lost, ffmpeg sees EOF), the time limit is
reached, free space drops under FREE_SPACE_FLOOR, or ffmpeg exits on its own.

Settings (player.yaml `recording:`): format mp3|opus|flac|original,
mp3_bitrate, opus_bitrate, flac_level, location auto|usb|sd, max_minutes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

FORMATS = {
    # id: (extension, ffmpeg muxer)
    "mp3": ("mp3", "mp3"),
    "opus": ("opus", "opus"),
    "flac": ("flac", "flac"),
    # The broadcast as received, no re-encode (Matroska audio holds any codec).
    "original": ("mka", "matroska"),
}
MP3_BITRATES = (128, 160, 192, 256, 320)
OPUS_BITRATES = (64, 96, 128, 160, 192, 256)
LOCATIONS = ("auto", "usb", "sd")
DEFAULTS = {
    "format": "mp3",
    "mp3_bitrate": 192,
    "opus_bitrate": 128,
    "flac_level": 5,
    "location": "auto",
    "max_minutes": 240,
}
AUDIO_EXTS = {"." + ext for ext, _ in FORMATS.values()}
FREE_SPACE_FLOOR = 200 * 1024 * 1024
FIFO = Path("/run/audio-player/record.mka")
LOG = Path("/run/audio-player/record.log")
USB_DIRNAME = "SAFYDO Recordings"


def _write_nofollow(path: Path, text: str) -> None:
    """Write a small file without following a symlink planted at its name
    (a USB stick is foreign media and the backend runs as root)."""
    if path.is_symlink():
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def _plain_file_in(f: Path, d: Path) -> bool:
    """A regular file sitting directly in d — not a symlink, not elsewhere."""
    try:
        return not f.is_symlink() and f.is_file() and f.resolve().parent == d.resolve()
    except OSError:
        return False


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _sidecar(audio: Path) -> Path:
    # "<file>.<ext>.json", not "<stem>.json": two recordings started in the same
    # minute in different formats share a stem and would share one sidecar.
    return audio.with_name(audio.name + ".json")


def _release_fifo_writer() -> None:
    """Let a writer stuck opening the FIFO through.

    mpv keeps stream-record set across files and opens it again for the next
    one, under its demuxer lock. If ffmpeg has already finished (the source
    changed, a playlist advanced, the backend restarted), nobody reads, that
    open never returns and mpv freezes; unlinking the FIFO does not wake it.
    A non-blocking reader opened and closed at once completes the open, mpv's
    first write then fails and it drops the recording instead of hanging.
    Harmless when nothing is waiting."""
    try:
        fd = os.open(FIFO, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return
    os.close(fd)


class RecorderError(ValueError):
    pass


def _safe_name(text: str) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", text or "")
    text = re.sub(r"\s+", " ", text)[:80]
    # Bytes too: ext4 allows 255 per name, and this one still gets a date,
    # " (n)", the extension and ".part"/".json" around it. 80 characters of
    # CJK or emoji are 240-320 bytes.
    text = text.encode("utf-8", "ignore")[:160].decode("utf-8", "ignore")
    return text.strip(" .") or "Recording"


def _meta_str(meta: dict, key: str) -> str | None:
    value = meta.get(key)
    return value if isinstance(value, str) else None


def _meta_num(meta: dict, key: str) -> float | None:
    value = meta.get(key)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class Recorder:
    def __init__(self, player, state_dir: Path, cfg_get) -> None:
        self.player = player
        self.sd_dir = state_dir / "recordings"
        self._cfg_get = cfg_get
        self._lock = threading.RLock()
        self._active: dict | None = None
        self._proc: subprocess.Popen | None = None
        self._last_error = ""
        # The .part this service started, so startup recovery only ever touches
        # its own file, never whatever happens to be on a USB stick.
        self._claim = state_dir / "recording_active.json"

    # ── settings ───────────────────────────────────────────────────────
    def settings(self) -> dict:
        out = dict(DEFAULTS)
        for key, default in DEFAULTS.items():
            raw = self._cfg_get(f"recording.{key}")
            if raw is None or raw == "":
                continue
            try:
                out[key] = validate({key: raw})[key]
            except RecorderError:
                out[key] = default
        return out

    def usb_dir(self) -> Path | None:
        mount = Path(self._cfg_get("paths.usb_mount") or "/media/usb")
        if not os.path.ismount(mount):
            return None
        d = mount / USB_DIRNAME
        # A stick could carry "SAFYDO Recordings" as a symlink to anywhere on
        # the Pi; root would then write and list there. Not ours: SD instead.
        if d.is_symlink():
            return None
        return d

    def target_dir(self, location: str | None = None) -> Path:
        location = location or self.settings()["location"]
        usb = self.usb_dir()
        if location == "usb":
            if not usb:
                raise RecorderError("No USB drive is mounted")
            return usb
        if location == "sd" or not usb:
            return self.sd_dir
        return usb

    # ── record ─────────────────────────────────────────────────────────
    def start(self) -> dict:
        with self._lock:
            if self._active:
                return self.status()
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise RecorderError("ffmpeg is not installed")
            mpv = self.player.mpv.status() if self.player.mpv.available() else {}
            if not mpv.get("running") or mpv.get("idle") or not mpv.get("path"):
                raise RecorderError("Nothing is playing")
            # Live only. --stream-record writes the packets mpv receives from
            # now on; a station, a live link or FM arrives in real time, but an
            # on-demand link or file is already read up to cache_secs ahead (a
            # short one entirely), so its recording would start well past what
            # you hear — or be empty.
            on_fm = self.player._on_radio_fifo(mpv)
            if not on_fm and not self.player._is_live():
                raise RecorderError(
                    "Recording is for live sources — stations, live links and FM. "
                    "This item has a fixed length and is already buffered ahead."
                )
            s = self.settings()
            out_dir = self.target_dir(s["location"])
            out_dir.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(out_dir).free < FREE_SPACE_FLOOR:
                raise RecorderError("Less than 200 MB free where recordings go")

            label = self._label(mpv)
            started = datetime.now()
            ext, muxer = FORMATS[s["format"]]
            base = f"{started:%Y-%m-%d %H%M} {_safe_name(label)}"
            final = out_dir / f"{base}.{ext}"
            n = 2
            while final.exists():
                final = out_dir / f"{base} ({n}).{ext}"
                n += 1
            part = final.with_name(final.name + ".part")
            # ffmpeg -y would write through anything planted at this
            # (predictable) name, symlinks included.
            if os.path.lexists(part):
                part.unlink()

            FIFO.parent.mkdir(parents=True, exist_ok=True)
            FIFO.unlink(missing_ok=True)
            os.mkfifo(FIFO, 0o600)
            cmd = [
                "nice", "-n", "10", ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-f", "matroska", "-i", str(FIFO), "-vn", "-map", "0:a:0",
                *self._codec_args(s),
                "-metadata", f"title={label} — {started:%Y-%m-%d %H:%M}",
                "-metadata", f"date={started:%Y-%m-%d}",
                "-metadata", "encoded_by=SAFYDO",
                "-f", muxer, str(part),
            ]
            # ffmpeg opens the FIFO for reading first; mpv's open for writing
            # then completes. stderr goes to a tmpfs log, not a pipe: a pipe
            # nobody drains fills up on a long recording and stalls ffmpeg.
            log = open(LOG, "w")  # noqa: SIM115 — handed to the child
            try:
                self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log)
            finally:
                log.close()
            try:
                # Cleared first, so mpv opens this FIFO afresh even if a backend
                # restart mid-recording left stream-record pointing at the path.
                self.player.mpv.set_property("stream-record", "")
                self.player.mpv.set_property("stream-record", str(FIFO))
            except Exception as exc:
                self._proc.kill()
                self._proc.wait()
                self._proc = None
                # A timeout here is mpv stuck opening the FIFO (ffmpeg died
                # before its end was open). Let it through while the path still
                # exists, then clear the setting: left set, the next file would
                # recreate the path as a plain file and record into tmpfs.
                _release_fifo_writer()
                try:
                    self.player.mpv.set_property("stream-record", "")
                except Exception:  # noqa: BLE001 — startup() clears it otherwise
                    pass
                FIFO.unlink(missing_ok=True)
                raise RecorderError(f"mpv refused to record: {exc}") from exc
            self._active = {
                "label": label,
                "path": str(final),
                "part": str(part),
                "source_path": mpv.get("path"),
                "source": self.player.saved_state().get("source"),
                "format": s["format"],
                "quality": self._quality_label(s),
                "started_at": time.time(),
                "max_minutes": s["max_minutes"],
            }
            self._last_error = ""
            try:
                self._claim.write_text(json.dumps({"part": str(part)}))
            except OSError:
                pass
            threading.Thread(target=self._monitor, name="recorder", daemon=True).start()
            return self.status()

    def stop_for_new_source(self) -> None:
        """MpvClient.before_replace: finish the recording before mpv loads
        something else (see the note there)."""
        if self._active:
            self.stop("the source changed")

    def stop(self, reason: str = "stopped") -> dict:
        with self._lock:
            active, proc = self._active, self._proc
            if not active:
                return self.status()
            # Taken now: the wait for ffmpeg below is not recorded time.
            duration = round(time.time() - active["started_at"], 1)
            # Before the IPC: an mpv stuck opening the FIFO answers nothing.
            _release_fifo_writer()
            try:
                self.player.mpv.set_property("stream-record", "")
            except Exception:  # noqa: BLE001 — mpv may be gone; ffmpeg gets EOF anyway
                pass
            if proc:
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
            try:
                err_lines = LOG.read_text().strip().splitlines()
            except OSError:
                err_lines = []
            _release_fifo_writer()
            FIFO.unlink(missing_ok=True)
            part, final = Path(active["part"]), Path(active["path"])
            try:
                self._claim.unlink(missing_ok=True)
                if _plain_file_in(part, part.parent) and part.stat().st_size > 0:
                    os.replace(part, final)
                    sidecar = {
                        "label": active["label"],
                        "source": active["source"],
                        "format": active["format"],
                        "quality": active["quality"],
                        "started_at": int(active["started_at"]),
                        "duration": duration,
                        "ended": reason,
                    }
                    _write_nofollow(_sidecar(final), json.dumps(sidecar, indent=2, ensure_ascii=False))
                else:
                    part.unlink(missing_ok=True)
                    self._last_error = err_lines[-1] if err_lines else "nothing was recorded"
            except OSError as exc:
                # The stick was pulled ("the disk went away"): EIO, not ENOENT.
                # The recording is over either way; only the file is lost.
                self._last_error = f"Could not save the recording: {exc.strerror or exc}"
            finally:
                # Always: a ghost _active made start() report a recording that
                # no longer existed, until the backend restarted.
                self._active, self._proc = None, None
            if reason not in ("stopped",) and not self._last_error:
                self._last_error = f"Recording ended: {reason}"
            return self.status()

    def _monitor(self) -> None:
        while True:
            time.sleep(2)
            with self._lock:
                active, proc = self._active, self._proc
                if not active or not proc:
                    return
                reason = None
                # An MpvError reads as None (mpv idle: the source is gone); a
                # socket error or timeout is "unknown", not a source change.
                try:
                    path = self.player.mpv.get_property_safe("path")
                    ended = bool(self.player.mpv.get_property_safe("eof-reached", False))
                    answered = True
                except Exception:  # noqa: BLE001
                    path, ended, answered = None, False, False
                # Source first: loading something else closes mpv's recording,
                # so ffmpeg has usually exited too by the time we look.
                if answered and path != active["source_path"]:
                    reason = "the source changed"
                elif proc.poll() is not None:
                    # Loading the next source closes the recording, and mpv can
                    # be too busy to answer mid-load: look once more. Guarded:
                    # an mpv frozen reopening the FIFO for the next playlist
                    # entry times out here, and an escaping timeout ended this
                    # thread with the recording "active" and mpv still stuck.
                    try:
                        again = self.player.mpv.get_property_safe("path")
                    except Exception:  # noqa: BLE001
                        again = None
                    reason = (
                        "the source changed"
                        if again is not None and again != active["source_path"]
                        else "the encoder stopped"
                    )
                elif ended:
                    # A finished web link or file: --keep-open holds mpv on its
                    # last frame with the same path, so nothing else would end it.
                    reason = "the source ended"
                else:
                    if active["max_minutes"] and time.time() - active["started_at"] >= active["max_minutes"] * 60:
                        reason = f"the {active['max_minutes']} min limit"
                    else:
                        try:
                            if shutil.disk_usage(Path(active["part"]).parent).free < FREE_SPACE_FLOOR // 2:
                                reason = "the disk is nearly full"
                        except OSError:
                            reason = "the disk went away"
                if reason:
                    self.stop(reason)
                    return

    def startup(self) -> int:
        """After a backend (re)start: clear what a recording cut short left in
        mpv (stream-record still pointing at the FIFO, which mpv would block
        opening for the next file) and in /run, then keep its .part."""
        _release_fifo_writer()
        try:
            if self.player.mpv.available():
                self.player.mpv.set_property("stream-record", "")
        except Exception:  # noqa: BLE001 — mpv may be restarting; it starts clean then
            pass
        FIFO.unlink(missing_ok=True)
        return self.recover_partials()

    def recover_partials(self) -> int:
        """Keep the .part a backend restart or power cut left behind mid-
        recording (systemd stops ffmpeg with the service). Only the file this
        service recorded as started is touched. MP3/FLAC/Matroska play fine
        truncated; an Ogg/Opus file only lacks its final page."""
        with self._lock:
            if self._active or not self._claim.is_file():
                return 0
            try:
                part = Path(json.loads(self._claim.read_text())["part"])
            except (OSError, json.JSONDecodeError, KeyError, TypeError):
                part = None
            if not part or not part.name.endswith(".part"):
                self._claim.unlink(missing_ok=True)
                return 0
            allowed = {d.resolve() for _, d in self._dirs() if d.is_dir()}
            if part.parent.resolve() not in allowed:
                # Its folder is not here (yet): after a power cut the USB stick
                # can mount after the backend starts. Keep the claim; list()
                # asks again once the stick is back.
                return 0
            self._claim.unlink(missing_ok=True)
            if not _plain_file_in(part, part.parent):
                return 0
            final = part.with_name(part.name[: -len(".part")])
            if part.stat().st_size == 0 or os.path.lexists(final):
                part.unlink(missing_ok=True)
                return 0
            os.replace(part, final)
            sidecar = {"label": final.stem, "format": final.suffix[1:], "ended": "interrupted"}
            _write_nofollow(_sidecar(final), json.dumps(sidecar, indent=2))
            return 1

    def status(self) -> dict:
        a = self._active
        if not a:
            return {"active": False, "error": self._last_error or None}
        part = Path(a["part"])
        try:
            size = part.stat().st_size
        except OSError:
            size = 0
        return {
            "active": True,
            "label": a["label"],
            "file": Path(a["path"]).name,
            "format": a["format"],
            "quality": a["quality"],
            "elapsed": round(time.time() - a["started_at"], 1),
            "bytes": size,
            "max_minutes": a["max_minutes"],
        }

    # ── browse ─────────────────────────────────────────────────────────
    def _dirs(self) -> list[tuple[str, Path]]:
        dirs = [("sd", self.sd_dir)]
        usb = self.usb_dir()
        if usb:
            dirs.insert(0, ("usb", usb))
        return dirs

    @staticmethod
    def _id(path: Path) -> str:
        return hashlib.sha1(str(path).encode()).hexdigest()[:16]

    def list(self) -> list[dict]:
        if not self._active and self._claim.is_file():
            self.recover_partials()  # a USB stick that mounted after startup
        items = []
        for where, d in self._dirs():
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if f.suffix.lower() not in AUDIO_EXTS or not _plain_file_in(f, d):
                    continue
                meta = {}
                side = _sidecar(f)
                if _plain_file_in(side, d):
                    try:
                        meta = json.loads(side.read_text())
                    except (OSError, ValueError):
                        meta = {}
                # Sidecars on a stick are foreign: a wrong type must not take
                # the whole list (or the UI rendering it) down.
                if not isinstance(meta, dict):
                    meta = {}
                try:
                    st = f.stat()
                except OSError:
                    continue
                items.append(
                    {
                        "id": self._id(f),
                        "name": f.stem,
                        "file": f.name,
                        "location": where,
                        "format": _meta_str(meta, "format") or f.suffix[1:],
                        "quality": _meta_str(meta, "quality"),
                        "label": _meta_str(meta, "label"),
                        "duration": _meta_num(meta, "duration"),
                        "bytes": st.st_size,
                        "created_at": int(_meta_num(meta, "started_at") or st.st_mtime),
                    }
                )
        items.sort(key=lambda i: i["created_at"], reverse=True)
        return items

    def resolve(self, rec_id: str) -> Path:
        for _, d in self._dirs():
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if f.suffix.lower() in AUDIO_EXTS and _plain_file_in(f, d) and self._id(f) == rec_id:
                    return f
        raise KeyError(rec_id)

    def delete(self, rec_id: str) -> None:
        f = self.resolve(rec_id)
        f.unlink()
        _sidecar(f).unlink(missing_ok=True)

    def rename(self, rec_id: str, name: str) -> dict:
        f = self.resolve(rec_id)
        clean = _safe_name(name)
        target = f.with_name(clean + f.suffix)
        # On a FAT stick "Show" and "show" are one name: a case-only rename
        # finds the file itself there, which is not a clash.
        if target != f and os.path.lexists(target) and not _same_file(target, f):
            raise RecorderError("A recording with that name already exists")
        os.replace(f, target)
        side = _sidecar(f)
        if _plain_file_in(side, f.parent):
            os.replace(side, _sidecar(target))
        return {"id": self._id(target), "name": clean}

    def free_space(self) -> dict:
        out = {}
        for where, d in self._dirs():
            probe = d if d.is_dir() else d.parent
            try:
                u = shutil.disk_usage(probe)
                out[where] = {"free": u.free, "total": u.total}
            except OSError:
                continue
        return out

    # ── helpers ────────────────────────────────────────────────────────
    def _label(self, mpv: dict) -> str:
        radio = getattr(self.player, "radio", None)
        try:
            if radio and self.player._on_radio_fifo(mpv):
                st = radio.summary()
                khz = st.get("frequency_khz") or 0
                name = (st.get("name") or "").strip()
                mhz = f"FM {khz / 1000:.1f}"
                return f"{mhz} {name}" if name and name != mhz else mhz
        except Exception:  # noqa: BLE001
            pass
        meta = self.player.saved_state().get("meta") or {}
        info = mpv.get("track_info") or {}
        return meta.get("name") or mpv.get("title") or info.get("title") or "Recording"

    @staticmethod
    def _codec_args(s: dict) -> list[str]:
        if s["format"] == "mp3":
            return ["-c:a", "libmp3lame", "-b:a", f"{s['mp3_bitrate']}k"]
        if s["format"] == "opus":
            return ["-c:a", "libopus", "-b:a", f"{s['opus_bitrate']}k", "-vbr", "on"]
        if s["format"] == "flac":
            # 16-bit: the decoders hand over float samples and the FLAC encoder
            # would keep 24 bits of them — bigger than raw PCM, for nothing
            # (the sources are 16-bit or lossy).
            return ["-c:a", "flac", "-sample_fmt", "s16", "-compression_level", str(s["flac_level"])]
        return ["-c:a", "copy"]

    @staticmethod
    def _quality_label(s: dict) -> str:
        return {
            "mp3": f"{s['mp3_bitrate']} kbps",
            "opus": f"{s['opus_bitrate']} kbps",
            "flac": f"level {s['flac_level']}",
            "original": "as broadcast",
        }[s["format"]]


def validate(patch: dict) -> dict:
    out = {}
    for key, value in patch.items():
        if key == "format":
            if value not in FORMATS:
                raise RecorderError(f"format: one of {', '.join(FORMATS)}")
            out[key] = value
        elif key == "location":
            if value not in LOCATIONS:
                raise RecorderError(f"location: one of {', '.join(LOCATIONS)}")
            out[key] = value
        elif key in ("mp3_bitrate", "opus_bitrate", "flac_level", "max_minutes"):
            try:
                v = int(value)
            except (TypeError, ValueError) as exc:
                raise RecorderError(f"{key}: not a number") from exc
            allowed = {
                "mp3_bitrate": MP3_BITRATES,
                "opus_bitrate": OPUS_BITRATES,
                "flac_level": range(0, 9),
                "max_minutes": range(0, 601),
            }[key]
            if v not in allowed:
                raise RecorderError(f"{key}: not an allowed value")
            out[key] = v
        else:
            raise RecorderError(f"unknown setting: {key}")
    return out

"""mpv JSON IPC client."""

from __future__ import annotations

import itertools
import json
import os
import re
import select
import socket
import threading
import time
from pathlib import Path
from typing import Any

from backend.textsafe import display, mpv_path


class MpvError(Exception):
    pass


class MpvClient:
    def __init__(self, socket_path: str | Path, timeout: float = 8.0) -> None:
        self._version: tuple[int, int] | None = None
        # Called before anything replaces the current file. The recorder hooks
        # in here: mpv keeps --stream-record across files and would reopen the
        # recording FIFO for the next one — with nobody reading it, that open
        # blocks and mpv stops answering. Stopping first, while mpv still
        # answers, means there is nothing left to reopen.
        self.before_replace = None
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        # One persistent connection, shared under a lock. A connection per
        # command made mpv start an IPC client thread for each one: 27 a second
        # on the 3 A+ (2026-11-20), costing mpv ~2 % and the backend ~1 % of a
        # core for nothing but setup and teardown.
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._sock_ino: int | None = None
        self._buf = b""
        self._ids = itertools.count(1)

    def available(self) -> bool:
        # Existence is NOT liveness: systemd only removes the socket with the
        # runtime directory, so a stale file outlives the process and made
        # restart_mpv_service() report success without ever waiting.
        ino = self._socket_ino()
        if ino is None:
            return False
        if self._sock is not None and ino == self._sock_ino:
            if self._peer_alive():
                return True
            # Killed mpv: the file stays, the connection is dead. Under the
            # lock so a command in flight is not cut off.
            with self._lock:
                if self._sock is not None and not self._peer_alive():
                    self._close()
        return self._probe_socket()

    def _peer_alive(self) -> bool:
        """No events are enabled on the shared connection, so with no command
        in flight it is readable only once mpv has hung up (EOF)."""
        sock = self._sock
        if sock is None:
            return False
        try:
            r, _, _ = select.select([sock], [], [], 0)
            if not r:
                return True
            return sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) != b""
        except BlockingIOError:
            return True
        except (OSError, ValueError):
            return False

    def _socket_ino(self) -> int | None:
        try:
            return os.stat(self.socket_path).st_ino
        except OSError:
            return None

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock, self._sock_ino, self._buf = None, None, b""

    def _connect(self) -> socket.socket:
        """The shared connection, (re)opened when absent or when the socket
        file was replaced (mpv restarted). Caller holds the lock."""
        ino = self._socket_ino()
        if self._sock is not None and ino is not None and ino == self._sock_ino:
            return self._sock
        self._close()
        if ino is None:
            raise MpvError(f"mpv socket not found: {self.socket_path}")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self.socket_path))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            sock.close()
            raise MpvError(f"mpv socket not found: {self.socket_path}") from exc
        except OSError:
            sock.close()
            raise
        self._sock, self._sock_ino = sock, ino
        # Nothing here listens for events; without this mpv queues every
        # start-file/audio-reconfig/seek on the connection between commands.
        try:
            self._send({"command": ["disable_event", "all"], "request_id": 0})
            self._recv_reply(0)
        except (OSError, MpvError):
            self._close()
            raise
        return sock

    def _send(self, msg: dict) -> None:
        assert self._sock is not None
        self._sock.sendall((json.dumps(msg) + "\n").encode())

    def _recv_reply(self, request_id: int) -> dict:
        """The reply carrying request_id. Event lines and stale replies (from
        a command whose wait timed out earlier) are skipped."""
        assert self._sock is not None
        while True:
            while b"\n" not in self._buf:
                part = self._sock.recv(65536)
                if not part:
                    raise MpvError("mpv closed the IPC connection")
                self._buf += part
            line, self._buf = self._buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                # surrogateescape: a station's ICY title can arrive in Latin-1
                # (Antenne Kärnten: byte 0xe4) and a file path in any bytes;
                # mpv passes both through raw. json.loads(bytes) raised
                # UnicodeDecodeError and /api/status answered 500. Raw bytes are
                # kept (paths must round-trip); textsafe.display() fixes them
                # wherever they are shown.
                msg = json.loads(line.decode("utf-8", errors="surrogateescape"))
            except json.JSONDecodeError as exc:
                raise MpvError(f"invalid mpv reply: {line.decode(errors='replace')!r}") from exc
            if not isinstance(msg, dict):
                raise MpvError(f"invalid mpv reply: {msg!r}")
            if "event" in msg and "error" not in msg:
                continue
            if msg.get("request_id") == request_id:
                return msg

    def _probe_socket(self) -> bool:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.4)
                sock.connect(str(self.socket_path))
            return True
        except OSError:
            return False

    def wait_available(self, timeout: float = 15.0, interval: float = 0.5) -> bool:
        """Poll until the IPC socket accepts connections.

        mpv_daemon.sh only creates the socket after the BlueALSA sink opens,
        which measured ~8 s on a Pi 3 A+ — a fixed short sleep declares failure
        while mpv is still coming up."""
        deadline = time.monotonic() + timeout
        while True:
            if self.available():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)

    def command(self, *args: Any) -> dict:
        """Run one IPC command on the shared connection.

        A missing or dead socket is an MpvError; a timeout stays an OSError
        (callers tell the two apart: gone vs. busy). The command is retried
        once only when sending it failed, i.e. mpv never received it (the old
        connection died with a restart), so a loadfile can never run twice.
        """
        if not self._lock.acquire(timeout=self.timeout):
            raise TimeoutError("mpv IPC busy")
        try:
            for attempt in (1, 2):
                self._connect()
                rid = next(self._ids)
                try:
                    self._send({"command": list(args), "request_id": rid})
                except OSError:
                    self._close()
                    if attempt == 2:
                        raise MpvError("mpv IPC connection lost")
                    continue
                try:
                    reply = self._recv_reply(rid)
                except (OSError, MpvError):
                    # Timed out or mpv went away mid-command: the stream state
                    # is unknown, start the next command on a fresh connection.
                    self._close()
                    raise
                break
        finally:
            self._lock.release()
        if reply.get("error") != "success":
            raise MpvError(str(reply.get("error", reply)))
        return reply

    def get_property(self, name: str) -> Any:
        reply = self.command("get_property", name)
        return reply.get("data")

    def get_property_safe(self, name: str, default: Any = None) -> Any:
        try:
            return self.get_property(name)
        except MpvError:
            return default

    def set_property(self, name: str, value: Any) -> None:
        self.command("set_property", name, value)

    def version(self) -> tuple[int, int]:
        """(major, minor) of the running mpv, e.g. (0, 40). Cached once read;
        an unreadable answer is not cached so a later call can retry."""
        if self._version:
            return self._version
        raw = str(self.get_property_safe("mpv-version") or "")
        m = re.search(r"(\d+)\.(\d+)", raw)
        if not m:
            return (0, 0)
        self._version = (int(m.group(1)), int(m.group(2)))
        return self._version

    def loadfile(
        self, target: str, mode: str = "replace", options: str | None = None, index: int | None = None
    ) -> None:
        """loadfile across mpv versions. Per-file options reset when the next
        file loads, so they never leak to other entries.

        mpv 0.38 put an <index> argument before <options> and added the
        insert-at flag: `loadfile URL FLAGS INDEX OPTIONS`. Bookworm's 0.35
        (Zero 2W) only knows `loadfile URL FLAGS OPTIONS`, so there insert-at
        is an append followed by playlist-move.
        """
        # A local file whose name is not UTF-8 cannot cross the JSON IPC as is:
        # mpv gets an ASCII symlink, and the readable name as its title so Now
        # Playing does not show the symlink's hash. %N% is mpv's length-quoting,
        # so commas or quotes in the name cannot break the option list.
        linked = mpv_path(target)
        if linked != target:
            title = display(Path(target).name)
            opt = f"force-media-title=%{len(title.encode('utf-8'))}%{title}"
            options = f"{options},{opt}" if options else opt
            target = linked
        if mode == "replace" and self.before_replace is not None:
            try:
                self.before_replace()
            except Exception:  # noqa: BLE001 — never block a load over this
                pass
        if self.version() >= (0, 38):
            args = [target, mode, -1 if index is None else index]
            if options:
                args.append(options)
            if not options and index is None:
                args = [target, mode]
            self.command("loadfile", *args)
            return
        legacy_mode = "append" if mode == "insert-at" else mode
        self.command("loadfile", target, legacy_mode, *([options] if options else []))
        if mode == "insert-at" and index is not None:
            count = int(self.get_property_safe("playlist-count") or 0)
            if count and index < count - 1:
                self.command("playlist-move", count - 1, index)

    def load(self, target: str, mode: str = "replace", options: str | None = None) -> None:
        self.loadfile(target, mode, options)

    def stop(self) -> None:
        self.command("stop")

    def pause(self, paused: bool = True) -> None:
        self.set_property("pause", paused)

    def set_volume(self, volume: int) -> None:
        self.set_property("volume", max(0, min(100, volume)))

    @staticmethod
    def _parse_bitrate(value: Any) -> int | None:
        if value is None or isinstance(value, (dict, list)):
            return None
        text = str(value).strip().lower()
        if not text:
            return None
        text = (
            text.replace("kbps", "")
            .replace("kb/s", "")
            .replace("kbit/s", "")
            .replace("bps", "")
            .strip()
        )
        match = re.search(r"(\d+(?:\.\d+)?)", text)
        if not match:
            return None
        try:
            num = float(match.group(1))
        except ValueError:
            return None
        # demux-bitrate and some ICY fields are bits per second (e.g. 128000).
        # Not from 1000 up: a lossless stream announces icy-br 1411 in kbps, and
        # no real stream runs under 10 kbps (station_lookup.sniff agrees).
        if num >= 10_000:
            num /= 1000.0
        kbps = int(round(num))
        if kbps <= 0 or kbps > 2000:
            return None
        return kbps

    @staticmethod
    def _parse_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            num = int(str(value).strip().replace(",", ""))
            return num if num >= 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_genres(value: Any) -> list[str]:
        if not value:
            return []
        text = str(value).strip()
        for sep in (";", "/", "|"):
            if sep in text:
                return [g.strip() for g in text.split(sep) if g.strip()]
        if "," in text:
            return [g.strip() for g in text.split(",") if g.strip()]
        return [text] if text else []

    def _metadata_dict(self) -> dict[str, str]:
        raw = self.get_property_safe("metadata")
        if isinstance(raw, dict):
            return {display(k, 80): display(v) for k, v in raw.items() if v is not None}
        return {}

    def _selected_audio_track(self) -> dict:
        tracks = self.get_property_safe("track-list") or []
        if not isinstance(tracks, list):
            return {}
        for track in tracks:
            if not isinstance(track, dict):
                continue
            if track.get("type") == "audio" and track.get("selected"):
                return track
        for track in tracks:
            if isinstance(track, dict) and track.get("type") == "audio":
                return track
        return {}

    def track_info(self) -> dict:
        if not self.available() or bool(self.get_property_safe("idle-active", True)):
            return {}
        meta = self._metadata_dict()
        audio = self._selected_audio_track()
        params = self.get_property_safe("audio-params") or {}
        if not isinstance(params, dict):
            params = {}

        title = (
            meta.get("StreamTitle")
            or meta.get("icy-title")
            or meta.get("title")
            or display(self.get_property_safe("media-title"))
        )
        description = (
            meta.get("icy-description")
            or meta.get("description")
            or meta.get("comment")
            or meta.get("album")
        )
        station = meta.get("icy-name") or meta.get("icy-title")
        genre_raw = meta.get("icy-genre") or meta.get("genre") or audio.get("genre")
        bitrate = (
            self._parse_bitrate(meta.get("icy-br"))
            or self._parse_bitrate(meta.get("icy-bitrate"))
            or self._parse_bitrate(audio.get("demux-bitrate"))
            or self._parse_bitrate(audio.get("bitrate"))
            or self._parse_bitrate(self.get_property_safe("audio-bitrate"))
            or self._parse_bitrate(description)
        )
        listeners = self._parse_int(meta.get("icy-listeners") or meta.get("listeners"))
        fmt = (
            audio.get("codec-desc")
            or audio.get("codec")
            or params.get("format")
            or meta.get("AudioFormat")
        )
        sample_rate = params.get("samplerate") or audio.get("audio-samplerate")
        channels = params.get("channel-count") or audio.get("audio-channels")

        return {
            k: v
            for k, v in {
                "title": title,
                "description": description,
                "station": station,
                "bitrate_kbps": bitrate,
                "genres": self._parse_genres(genre_raw),
                "listeners": listeners,
                "format": fmt,
                "sample_rate": int(sample_rate) if sample_rate else None,
                "channels": int(channels) if channels else None,
            }.items()
            if v not in (None, "", [], {})
        }

    def status(self) -> dict:
        if not self.available():
            return {"running": False, "idle": True}
        idle = bool(self.get_property_safe("idle-active", True))
        payload = {
            "running": True,
            "idle": idle,
            "paused": bool(self.get_property_safe("pause", False)),
            "volume": self.get_property_safe("volume"),
            "duration": self.get_property_safe("duration"),
            "position": self.get_property_safe("playback-time"),
            "seekable": self.get_property_safe("seekable"),
            **self._cache_summary(),
            "playlist_pos": self.get_property_safe("playlist-pos"),
            "playlist_count": self.get_property_safe("playlist-count"),
            "title": display(self.get_property_safe("media-title")) or None,
            "path": self.get_property_safe("path"),
            "audio_device": self.get_property_safe("audio-device"),
        }
        if not idle:
            payload["track_info"] = self.track_info()
        return payload

    def _cache_ranges(self) -> list[list[float]]:
        return self._cache_summary()["cache_ranges"]

    def _cache_summary(self) -> dict:
        """Seekable spans held in the demuxer cache, [[start, end], ...] in the
        same timebase as playback-time (on a live stream: the rewind window,
        its last end the live edge), and the bytes the cache holds for them."""
        state = self.get_property_safe("demuxer-cache-state") or {}
        if not isinstance(state, dict):
            state = {}
        ranges = []
        for r in state.get("seekable-ranges") or []:
            try:
                ranges.append([float(r["start"]), float(r["end"])])
            except (KeyError, TypeError, ValueError):
                continue
        try:
            total = int(state.get("total-bytes") or 0)
        except (TypeError, ValueError):
            total = 0
        return {"cache_ranges": ranges, "cache_bytes": total}

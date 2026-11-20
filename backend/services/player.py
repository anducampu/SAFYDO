"""Core player — streams, USB files, mpv IPC."""

from __future__ import annotations

import json
import re
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from backend.services.audio_verify import (
    bt_connected,
    ensure_playback_ready,
    restart_mpv_service,
    verify_audio,
)
from backend.services import station_lookup
from backend.services.eq import EqService
from backend.services.links import IMPERSONATE, platform_of
from backend.services.mpv import MpvClient, MpvError
from backend.textsafe import display
from backend.services.spectrum import SpectrumTap
from backend.services.volume_hw import read_hw_volume, set_hw_volume, speaker_has_remote_volume

# Where the speaker starts when sync first takes over from software attenuation.
SAFE_SPEAKER_LEVEL = 20
VOLUME_REPROBE_S = 600.0

# Rewind window on live streams = mpv's back buffer (demuxer-max-back-bytes,
# RAM). Sized from the stream's bitrate, capped so a long window on a
# high-bitrate stream cannot crowd a 512 MB board.
REWIND_DEFAULT_MIN = 10
# 96 MB holds 30 min up to ~430 kbps of measured cache cost (a 168 kbps MP3
# station costs ~320). Full, it takes a 472 MB 3 A+ to ~280 MB used.
REWIND_CAP_BYTES = 96 * 1024 * 1024
REWIND_FALLBACK_KBPS = 192
# "Live" sits this far behind the newest cached byte: what mpv keeps ahead of
# playback anyway (measured ~13 s on a 160 kbps stream). Jumping to the very
# end would leave no buffer and the first network hiccup would be audible.
LIVE_LEAD_S = 10.0

AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".wav", ".m4a", ".aac", ".opus", ".wma", ".aiff", ".aif"}


def _normalize_genres(raw) -> str:
    """One or more genres, comma-separated: "ambient,chill ;Chill" -> "AMBIENT, CHILL".
    The UI splits on the comma, so a station sits under every genre it lists."""
    seen: list[str] = []
    for part in str(raw or "").replace(";", ",").split(","):
        g = part.strip().upper()
        if g and g not in seen:
            seen.append(g)
    return ", ".join(seen) or "VA"


class PlayerService:
    def __init__(
        self,
        install_dir: Path,
        state_dir: Path,
        mpv_socket: Path,
        cfg_get,
        cfg_set,
    ) -> None:
        self.install_dir = install_dir
        self.state_dir = state_dir
        self.mpv = MpvClient(mpv_socket)
        self.streams_path = install_dir / "config" / "streams.json"
        self.usb_mount = Path(cfg_get("paths.usb_mount") or "/media/usb")
        self.state_file = state_dir / "player_state.json"
        self.usb_cache_file = state_dir / "usb_tracks.json"
        self.eq = EqService(
            install_dir / "config" / "eq_presets.json",
            cfg_get,
            cfg_set,
            self.mpv,
        )
        self.spectrum = SpectrumTap(self.mpv)
        self._viz_active = False
        self.radio = None  # RadioService, wired in backend.config
        self.links = None  # LinksService, wired in backend.config
        self.recorder = None  # Recorder, wired in backend.config
        self._volume_ignored_by: str | None = None
        self._volume_ignored_at = 0.0
        self._volume_lock = threading.Lock()
        # streams.json is read-modify-written from request threads and the logo
        # batch; one lock keeps a logo write from racing an editor save.
        self._streams_lock = threading.RLock()
        self._cfg_get = cfg_get
        self._cfg_set = cfg_set
        # Rewind sizing per playing file: highest bytes/s seen, budget last set.
        self._rewind_path: str | None = None
        self._rewind_bps = 0.0
        self._rewind_source = "fallback"
        self._rewind_budget = 0
        # Set when mpv's property may no longer match _rewind_budget, so the
        # next apply_rewind() writes it. Not budget == 0: with rewind set to 0
        # minutes that is the real budget, and it was rewritten every second.
        self._rewind_stale = True

    @staticmethod
    def _is_disabled(s: dict) -> bool:
        return bool(s.get("disabled") or s.get("disable"))

    def list_streams(self, favorites_only: bool = False, include_disabled: bool = False) -> list[dict]:
        data = json.loads(self.streams_path.read_text())
        streams = data.get("streams") or []
        if not include_disabled:
            streams = [s for s in streams if not self._is_disabled(s)]
        if favorites_only:
            streams = [s for s in streams if s.get("favorite")]
        return streams

    def get_stream(self, stream_id: str) -> dict | None:
        # Resolve any station by id (even disabled) so resume/id-play still work.
        for stream in self.list_streams(include_disabled=True):
            if stream.get("id") == stream_id:
                return stream
        return None

    def _write_streams(self, streams: list[dict]) -> None:
        with self._streams_lock:
            tmp = self.streams_path.with_suffix(".json.part")
            tmp.write_text(json.dumps({"streams": streams}, indent=2, ensure_ascii=False) + "\n")
            # Written atomically (power loss mid-write must not empty the list),
            # keeping the original owner/mode so it stays editable over SSH.
            try:
                st = self.streams_path.stat()
                os.chmod(tmp, st.st_mode & 0o7777)
                os.chown(tmp, st.st_uid, st.st_gid)
            except OSError:
                pass
            tmp.replace(self.streams_path)
            station_lookup.prune_logos({str(s.get("logo")) for s in streams if s.get("logo")})

    @staticmethod
    def _slug(text: str) -> str:
        s = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
        return s or "stream"

    def _clean_stream(self, data: dict) -> dict:
        name = str(data.get("name", "")).strip()
        url = str(data.get("url", "")).strip()
        if not name:
            raise ValueError("name is required")
        if urlparse(url).scheme not in ("http", "https"):
            raise ValueError(f"url must be http(s): {url or '(empty)'}")
        out = {
            "name": name,
            "url": url,
            "genre": _normalize_genres(data.get("genre")),
            "category": (str(data.get("category", "") or "").strip() or "Radio"),
            "favorite": bool(data.get("favorite", False)),
        }
        # Optional fields, carried only when set so the JSON stays tidy.
        homepage = str(data.get("homepage") or "").strip()
        if homepage and urlparse(homepage).scheme in ("http", "https"):
            out["homepage"] = homepage
        color = str(data.get("color") or "").strip().lower()
        if re.fullmatch(r"#[0-9a-f]{6}", color):
            out["color"] = color
        logo = str(data.get("logo") or "").strip()
        if station_lookup.LOGO_NAME_RE.match(logo):
            out["logo"] = logo
        if self._is_disabled(data):
            out["disabled"] = True
        return out

    def update_stream(self, stream_id: str, fields: dict) -> dict | None:
        """Merges [fields] into one station (validated like any edit); None if unknown."""
        with self._streams_lock:
            streams = self.list_streams(include_disabled=True)
            for i, s in enumerate(streams):
                if s.get("id") == stream_id:
                    merged = {k: v for k, v in {**s, **fields}.items() if v is not None}
                    streams[i] = {"id": stream_id, **self._clean_stream(merged)}
                    self._write_streams(streams)
                    return streams[i]
        return None

    def set_stream_logo(self, stream_id: str, found: dict | None) -> dict | None:
        """Stores a logo found by station_lookup.find_logo (None clears it).
        File write and reference happen under one lock, so a concurrent save
        can't prune the file between the two."""
        with self._streams_lock:
            if self.get_stream(stream_id) is None:
                return None
            name = station_lookup.store_logo(found) if found else ""
            return self.update_stream(stream_id, {"logo": name})

    def add_stream(self, data: dict) -> dict:
        with self._streams_lock:
            return self._add_stream(data)

    def _add_stream(self, data: dict) -> dict:
        streams = self.list_streams(include_disabled=True)
        clean = self._clean_stream(data)
        ids = {s.get("id") for s in streams}
        base = self._slug(clean["name"])
        sid, n = base, 2
        while sid in ids:
            sid, n = f"{base}-{n}", n + 1
        stream = {"id": sid, **clean}
        streams.append(stream)
        self._write_streams(streams)
        return stream

    def replace_streams(self, streams_in: list) -> list[dict]:
        with self._streams_lock:
            return self._replace_streams(streams_in)

    def _replace_streams(self, streams_in: list) -> list[dict]:
        if not isinstance(streams_in, list):
            raise ValueError("streams must be a list")
        out: list[dict] = []
        ids: set[str] = set()
        for item in streams_in:
            if not isinstance(item, dict):
                raise ValueError("each stream must be an object")
            clean = self._clean_stream(item)
            sid = str(item.get("id") or "").strip() or self._slug(clean["name"])
            base, n = sid, 2
            while sid in ids:
                sid, n = f"{base}-{n}", n + 1
            ids.add(sid)
            out.append({"id": sid, **clean})
        self._write_streams(out)
        return out

    def delete_stream(self, stream_id: str) -> bool:
        with self._streams_lock:
            return self._delete_stream(stream_id)

    def _delete_stream(self, stream_id: str) -> bool:
        streams = self.list_streams(include_disabled=True)
        kept = [s for s in streams if s.get("id") != stream_id]
        if len(kept) == len(streams):
            return False
        self._write_streams(kept)
        return True

    def play_stream(self, stream_id: str) -> dict:
        stream = self.get_stream(stream_id)
        if not stream:
            raise ValueError(f"unknown stream: {stream_id}")
        return self.play_url(stream["url"], meta=stream)

    def play_url(self, url: str, meta: dict | None = None) -> dict:
        parsed = urlparse(url.strip())
        if parsed.scheme in ("", "file"):
            local = Path(parsed.path if parsed.scheme == "file" else url)
            return self.play_file(str(local), meta=meta)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"unsupported URL scheme: {parsed.scheme or '(none)'}")
        # No _ensure_mpv() here on purpose: ensure_playback_ready() is strictly
        # more capable — it connects Bluetooth first and only then restarts mpv.
        # Guarding ahead of it meant a missing socket raised before the routine
        # that knows how to repair it ever ran.
        # Streams over congested 2.4 GHz (BT coexistence) can take >5 s to
        # open — give them longer before declaring failure.
        return self._play_remote(url, url, "stream", meta, playback_timeout=12.0)

    def play_link(self, url: str, meta: dict | None = None) -> dict:
        """A YouTube/SoundCloud/... page, played through mpv's ytdl_hook.

        The ytdl:// prefix sends it to yt-dlp straight away. Without it mpv
        first tries the page as a media file, fails, and only then falls back —
        one wasted HTTP fetch plus a demuxer probe on every play.
        """
        if urlparse(url).scheme not in ("http", "https"):
            raise ValueError("link must be an http(s) URL")
        # yt-dlp has to resolve the page (and, on YouTube, solve a JS challenge)
        # before mpv can even open the audio: measured in seconds on a Pi.
        return self._play_remote(
            f"ytdl://{url}", url, "link", meta, playback_timeout=30.0, options=self._link_options(url)
        )

    def _link_options(self, url: str) -> str | None:
        """Per-file mpv options for one web link. Account cookies (Vimeo) and
        browser impersonation (Rumble) apply to that entry only and reset when
        the next one loads — also inside a playlist mixing platforms."""
        cookies = self.links.cookies_for(url) if self.links else None
        raw = []
        if cookies:
            raw.append(f"cookies={cookies}")
        impersonate = IMPERSONATE.get(platform_of(url) or "")
        if impersonate:
            raw.append(f"impersonate={impersonate}")
        # mpv key/value list: entries joined by ",".
        return f"ytdl-raw-options={','.join(raw)}" if raw else None

    def play_link_playlist(
        self, links: list[dict], meta: dict, *, start: int = 0, shuffle: bool = False, loop: bool = False
    ) -> dict:
        """Load a saved playlist into mpv's own playlist. mpv resolves each
        entry through ytdl_hook only when it becomes current, so a long list
        costs nothing up front. The chosen entry loads first (it starts while
        the rest are queued around it, in order)."""
        if not links:
            raise ValueError("this playlist is empty")
        start = max(0, min(start, len(links) - 1))
        ensure_playback_ready(self.mpv, self._cfg_get, self.install_dir)
        self._leave_radio()
        first = links[start]
        self.mpv.load(f"ytdl://{first['url']}", "replace", self._link_options(first["url"]))
        for i, link in enumerate(links[:start]):
            # insert-at keeps the list in playlist order around the started entry
            self.mpv.loadfile(f"ytdl://{link['url']}", "insert-at", self._link_options(link["url"]), index=i)
        for link in links[start + 1 :]:
            self.mpv.loadfile(f"ytdl://{link['url']}", "append", self._link_options(link["url"]))
        self.mpv.set_property("loop-playlist", "inf" if loop else "no")
        if shuffle:
            self.mpv.command("playlist-shuffle")
        self.mpv.pause(False)
        state = self._write_state(source="link_playlist", url=None, meta=meta)
        self._verify_playback(state, playback_timeout=30.0)
        self.refresh_audio_filters()
        self.apply_rewind()
        return state

    def set_playlist_modes(
        self, links: list[dict], *, shuffle: bool | None = None, loop: bool | None = None
    ) -> None:
        """Apply a playlist's shuffle/loop change to what is playing now."""
        if loop is not None:
            self.mpv.set_property("loop-playlist", "inf" if loop else "no")
        if shuffle:
            self.mpv.command("playlist-shuffle")
        elif shuffle is False:
            # Not playlist-unshuffle: that restores mpv's order from before the
            # shuffle, which misses any edit made since. The saved order is truth.
            self.sync_playlist(links, reorder=True)

    def sync_playlist(self, links: list[dict], *, reorder: bool) -> None:
        """Make mpv's queue match a saved playlist without interrupting the
        entry that is playing: drop removed entries, append new ones, and
        (unless shuffled) move everything into the saved order.

        An entry removed while it plays keeps playing (removing it would cut
        it off); it is moved past the end so the queue stops — or loops —
        after it, as if it had been the last one.
        """
        wanted = [f"ytdl://{l['url']}" for l in links]
        by_target = {f"ytdl://{l['url']}": l for l in links}
        queue = self._queue()
        current = next((i for i, e in enumerate(queue) if e.get("current")), None)
        current_file = queue[current]["filename"] if current is not None else None

        # Remove from the end so earlier indices stay valid.
        for i in range(len(queue) - 1, -1, -1):
            name = queue[i]["filename"]
            if name not in by_target and name != current_file:
                self.mpv.command("playlist-remove", i)
        present = {e["filename"] for e in self._queue()}
        for name in wanted:
            if name not in present:
                url = by_target[name]["url"]
                self.mpv.loadfile(name, "append", self._link_options(url))
        if not reorder:
            return
        for target_index, name in enumerate(wanted):
            names = [e["filename"] for e in self._queue()]
            j = names.index(name)
            if j != target_index:
                # j > target_index always here (0..target_index-1 are settled),
                # so the entry lands exactly at target_index.
                self.mpv.command("playlist-move", j, target_index)

    def _queue(self) -> list[dict]:
        queue = self.mpv.get_property_safe("playlist") or []
        return [e for e in queue if isinstance(e, dict) and "filename" in e]

    def playlist_step(self, forward: bool) -> dict:
        self._refuse_pause_on_radio()
        try:
            self.mpv.command("playlist-next" if forward else "playlist-prev", "weak")
        except MpvError as exc:
            raise ValueError("no further entry in the playlist") from exc
        return self.status()

    def _play_remote(
        self,
        target: str,
        url: str,
        source: str,
        meta: dict | None,
        *,
        playback_timeout: float,
        options: str | None = None,
    ) -> dict:
        # No _ensure_mpv() here on purpose: ensure_playback_ready() is strictly
        # more capable — it connects Bluetooth first and only then restarts mpv.
        ensure_playback_ready(self.mpv, self._cfg_get, self.install_dir)
        self._leave_radio()
        self._load_single(target, options)
        self.mpv.pause(False)
        # default False: a momentarily busy IPC read must not trigger a
        # spurious stop+reload (double teardown audible as a long gap)
        if bool(self.mpv.get_property_safe("pause", False)):
            self.mpv.stop()
            self._load_single(target, options)
            self.mpv.pause(False)
        state = self._write_state(source=source, url=url, meta=meta or {})
        self._verify_playback(state, playback_timeout=playback_timeout)
        self.refresh_audio_filters()
        self.apply_rewind()
        return state

    def _load_single(self, target: str, options: str | None = None) -> None:
        """Replace whatever plays with one source. loop-playlist is global in
        mpv: left at a looped link playlist's "inf", a single file played next
        would repeat forever."""
        self.mpv.load(target, "replace", options)
        self.mpv.set_property("loop-playlist", "no")

    def apply_stream_settings(self, settings: dict) -> dict:
        """Push Settings -> Streams into the running mpv (mpv_daemon.sh passes
        the same values as flags when mpv starts)."""
        from backend.services import stream_settings

        if self.mpv.available():
            for prop, value in stream_settings.mpv_properties(settings):
                try:
                    self.mpv.set_property(prop, value)
                except MpvError:
                    pass  # one stubborn property must not block the rest
        return settings

    def rewind_minutes(self) -> int:
        try:
            return max(0, int(self._cfg_get("audio.rewind_minutes") or REWIND_DEFAULT_MIN))
        except (TypeError, ValueError):
            return REWIND_DEFAULT_MIN

    def apply_rewind(self, mpv_status: dict | None = None) -> int:
        """Size mpv's back buffer (RAM) to hold the configured rewind window.

        The rate is what the cache really costs: bytes held / seconds held,
        read from demuxer-cache-state once 20 s are in, so container overhead
        and VBR are already counted. Before that, the stream's declared
        bitrate +10 %; without one, 192 kbps. The highest rate seen for the
        file wins (VBR dips must not shrink the window under the user), and
        it starts over when the file changes (a playlist advancing). Called
        on every play, on a settings change, and from status() at 1 Hz;
        mpv is only touched when the budget moves by more than 5 %.
        """
        if mpv_status is None:
            mpv_status = self.mpv.status() if self.mpv.available() else {}
        if self._on_radio_fifo(mpv_status):
            return self._rewind_budget
        path = mpv_status.get("path")
        if path != self._rewind_path:
            self._rewind_path, self._rewind_bps, self._rewind_source = path, 0.0, "fallback"
            # Force a write: something else (radio.py leaving FM) may have
            # changed the property behind our cached budget.
            self._rewind_stale = True
        bps, source = self._measured_bps(mpv_status)
        rank = {"fallback": 0, "declared": 1, "measured": 2}
        if bps:
            better_source = rank[source] > rank[self._rewind_source]
            same_source_higher = source == self._rewind_source and bps > self._rewind_bps
            # A measurement replaces an estimate outright (even if lower);
            # within one kind, only a higher rate counts.
            if better_source or same_source_higher:
                self._rewind_bps, self._rewind_source = bps, source
        rate = self._rewind_bps or REWIND_FALLBACK_KBPS * 1000 / 8 * 1.1
        budget = int(min(REWIND_CAP_BYTES, self.rewind_minutes() * 60 * rate))
        if self._rewind_stale or abs(budget - self._rewind_budget) > 0.05 * self._rewind_budget:
            try:
                self.mpv.set_property("demuxer-max-back-bytes", budget)
                self._rewind_budget = budget
                self._rewind_stale = False
            except MpvError:
                pass
        return self._rewind_budget

    @staticmethod
    def _on_radio_fifo(mpv_status: dict) -> bool:
        from backend.services.radio import FIFO

        return str(mpv_status.get("path") or "") == str(FIFO)

    @staticmethod
    def _measured_bps(mpv_status: dict) -> tuple[float, str]:
        ranges = mpv_status.get("cache_ranges") or []
        held = sum(end - start for start, end in ranges)
        cached = mpv_status.get("cache_bytes") or 0
        if held >= 20 and cached > 0:
            return cached / held, "measured"
        kbps = (mpv_status.get("track_info") or {}).get("bitrate_kbps")
        if kbps:
            return kbps * 1000 / 8 * 1.1, "declared"
        return 0.0, "fallback"

    def rewind_info(self) -> dict:
        return {
            "rewind_bytes": self._rewind_budget,
            "rewind_kbps": round(self._rewind_bps * 8 / 1000) if self._rewind_bps else None,
            "rewind_rate_source": self._rewind_source,
            # What the budget really buys at this rate — less than asked when
            # the cap bites.
            "rewind_effective_minutes": (
                round(self._rewind_budget / self._rewind_bps / 60, 1)
                if self._rewind_bps and self._rewind_budget
                else None
            ),
        }

    def seek(self, position: float) -> dict:
        """Jump to an absolute position (s). With a length (link, local file)
        anywhere in it; on a live stream only inside the cached rewind window,
        where mpv can serve the seek from RAM."""
        self._refuse_pause_on_radio()
        duration = self.mpv.get_property_safe("duration")
        ranges = self.mpv._cache_ranges()
        if duration and not self._is_live():
            target = max(0.0, min(float(position), float(duration) - 1))
        elif ranges:
            start, end = ranges[0][0], ranges[-1][1]
            target = max(start, min(float(position), end - 1.0))
        else:
            raise ValueError("nothing seekable is playing")
        self.mpv.command("seek", target, "absolute")
        return self.status()

    def go_live(self) -> dict:
        """Back to the live edge: the end of the cached data."""
        self._refuse_pause_on_radio()
        ranges = self.mpv._cache_ranges()
        if not ranges:
            raise ValueError("no live buffer to jump in")
        self.mpv.command("seek", max(ranges[-1][0], ranges[-1][1] - LIVE_LEAD_S), "absolute")
        self.mpv.pause(False)
        return self.status()

    def _is_live(self, saved: dict | None = None) -> bool:
        # Not from mpv's duration: on a live HTTP stream it reports the length
        # of the cache (it grows), so it cannot tell live from finite.
        saved = self.saved_state() if saved is None else saved
        meta = saved.get("meta") or {}
        # Said explicitly by whoever started it (a directory knows a Jamendo
        # track is a file); otherwise a "stream" is radio — the station list,
        # a pasted URL — and anything else is finite.
        if isinstance(meta.get("is_live"), bool):
            return meta["is_live"]
        return saved.get("source") == "stream"

    def play_file(self, path: str, meta: dict | None = None) -> dict:
        file_path = self._resolve_usb_path(path)
        # No _ensure_mpv() here on purpose: ensure_playback_ready() is strictly
        # more capable — it connects Bluetooth first and only then restarts mpv.
        # Guarding ahead of it meant a missing socket raised before the routine
        # that knows how to repair it ever ran.
        ensure_playback_ready(self.mpv, self._cfg_get, self.install_dir)
        self._leave_radio()
        self._load_single(str(file_path))
        self.mpv.pause(False)
        file_meta = {"filename": display(file_path.name), **(meta or {})}
        state = self._write_state(source="usb", path=str(file_path), meta=file_meta)
        self._verify_playback(state, playback_timeout=6.0)
        self.refresh_audio_filters()
        return state

    def play_recording(self, path: Path, meta: dict | None = None) -> dict:
        """A file from the Recordings browser (the caller resolved the id, so
        the path is inside a recordings folder)."""
        ensure_playback_ready(self.mpv, self._cfg_get, self.install_dir)
        self._leave_radio()
        self._load_single(str(path))
        self.mpv.pause(False)
        state = self._write_state(source="recording", path=str(path), meta={"genre": "Recording", **(meta or {})})
        self._verify_playback(state, playback_timeout=6.0)
        self.refresh_audio_filters()
        return state

    def _verify_playback(self, state: dict, *, playback_timeout: float) -> None:
        """Raise only on real failures. "Not playing yet but mpv is still
        opening the source" (idle=False) is returned as a warning instead of
        a 503 — the stream usually starts a moment later and the old hard
        failure showed a UI error over working audio."""
        check = verify_audio(
            self.mpv,
            self._cfg_get,
            self.install_dir,
            require_playback=True,
            playback_timeout=playback_timeout,
        )
        state["audio_check"] = check
        if check["ok"]:
            return
        still_opening = set(check["failures"]) == {"playback_not_active"} and not check[
            "mpv"
        ]["idle"]
        if still_opening:
            check["warnings"].append("playback_still_buffering")
            return
        raise MpvError(f"playback verify failed: {', '.join(check['failures'])}")

    def _viz_filters_present(self) -> bool:
        if not self.mpv.available():
            return False
        af = self.mpv.get_property_safe("af") or []
        if not isinstance(af, list):
            return False
        for entry in af:
            if not isinstance(entry, dict) or entry.get("name") != "lavfi":
                continue
            graph = str((entry.get("params") or {}).get("graph", ""))
            # "viztap" identifies the current multi-band graph; a bare
            # "astats" match would also accept the legacy single-RMS chain
            # and skip upgrading it.
            if "viztap" in graph:
                return True
        return False

    def set_viz_active(self, active: bool) -> None:
        self._viz_active = active
        if active:
            self.ensure_viz_filters()
        else:
            self.spectrum.set_attached(False)
            self.refresh_audio_filters()

    def ensure_viz_filters(self) -> None:
        if not self._viz_active:
            return
        if not self._viz_filters_present():
            self.refresh_audio_filters()
            self.spectrum.set_attached(True)

    def refresh_audio_filters(self) -> None:
        self.eq.apply(viz_active=self._viz_active, viz=self.spectrum)

    def verify(self, test_tone: bool = False, require_playback: bool = False) -> dict:
        self._ensure_mpv()
        return verify_audio(
            self.mpv,
            self._cfg_get,
            self.install_dir,
            test_tone=test_tone,
            require_playback=require_playback,
        )

    def _leave_radio(self) -> None:
        if self.radio is not None:
            self.radio.release()
            # release() puts demuxer-max-back-bytes back to 50 MiB behind the
            # cached budget, and apply_rewind() skips FM, so its path check
            # misses that when the next stream is the one played before FM.
            # Stale makes the next apply_rewind() write the budget again.
            self._rewind_stale = True

    def _refuse_pause_on_radio(self) -> None:
        # The capture keeps running, so unpausing would play stale audio.
        if self.radio is not None and self.radio.listening:
            raise ValueError("Live radio cannot be paused; use Stop")

    def pause(self, paused: bool = True) -> dict:
        self._refuse_pause_on_radio()
        self._ensure_mpv()
        if not paused and bool(self.mpv.get_property_safe("eof-reached", False)):
            self.mpv.command("seek", 0, "absolute")
        self.mpv.pause(paused)
        return self.status()

    def toggle_pause(self) -> dict:
        self._refuse_pause_on_radio()
        self._ensure_mpv()
        current = bool(self.mpv.get_property_safe("pause", True))
        self.mpv.pause(not current)
        return self.status()

    def stop(self) -> dict:
        self._leave_radio()
        if self.mpv.available():
            self.mpv.stop()
        return self.status()

    def set_volume(self, volume: int) -> dict:
        volume = max(0, min(100, volume))
        mac = self._cfg_get("bluetooth.primary_mac")
        # Serialised: the read-back below must see this write and not a later one.
        with self._volume_lock:
            self._cfg_set("audio.volume", str(volume))
            if self._volume_follow_enabled():
                self._forget_stale_refusal(mac)
                self._set_speaker_or_software(mac, volume)
            elif self.mpv.available():
                self.mpv.set_volume(volume)
        return self.status()

    def _set_speaker_or_software(self, mac, volume: int) -> None:
        if self._volume_ignored_by == mac or not mac:
            if self.mpv.available():
                self.mpv.set_volume(volume)
            return
        already = str(self._cfg_get("audio.speaker_volume_mac") or "") == mac
        target = volume if already else min(volume, SAFE_SPEAKER_LEVEL)
        result = self._write_speaker_volume(mac, target)
        if result == "ignored":
            self._use_software_volume(mac)
            return
        if result == "unknown" and speaker_has_remote_volume(mac) is False:
            # No AVRCP volume at all (IKEA KALLSUP): the write can't land, and
            # until now the slider then did nothing. mpv is the only volume there is.
            if self.mpv.available():
                self.mpv.set_volume(volume)
            return
        if result == "ok":
            self._cfg_set("audio.volume", str(target))
            self._last_hw_volume = (mac, target)
            self._cfg_set("audio.speaker_volume_mac", mac)
            self._full_software_level()

    def _write_speaker_volume(self, mac, level: int) -> str:
        """Set the speaker's own volume and read it back: ok, ignored or unknown.

        BlueZ reflects an accepted write in this property, so "ok" means the speaker
        was told, and a speaker that acknowledges without acting still passes. Only
        a real reading that disagrees counts as refusal: no reading at all (the link
        just went, say) proves nothing and must not condemn the speaker.
        """
        if not set_hw_volume(mac, level):
            return "unknown"
        seen = None
        for _ in range(5):
            time.sleep(0.2)
            hw = read_hw_volume(mac)
            if hw is None:
                continue
            seen = hw
            if abs(hw - level) <= 1:
                return "ok"
        return "ignored" if seen is not None else "unknown"

    def _use_software_volume(self, mac) -> None:
        self._volume_ignored_by = mac
        self._volume_ignored_at = time.monotonic()
        self._cfg_set("audio.speaker_volume_mac", "")
        if self.mpv.available():
            self.mpv.set_volume(int(self._cfg_get("audio.volume") or 80))

    def _forget_stale_refusal(self, mac) -> None:
        # Worth asking again from time to time: the refusal may have been a one-off.
        if (
            self._volume_ignored_by == mac
            and time.monotonic() - self._volume_ignored_at > VOLUME_REPROBE_S
        ):
            self._volume_ignored_by = None

    def _full_software_level(self) -> None:
        if self.mpv.available():
            self.mpv.set_volume(100)

    def _enter_speaker_volume(self, mac, hw: int) -> bool:
        """One volume, in one place: the speaker's, with mpv at full level.

        Attenuating in both would make the slider drop twice as fast, and throw away
        resolution in software for nothing. audio.speaker_volume_mac records that a
        speaker has taken over, so only the first time brings it down to a safe
        level: raising mpv alone would be a sudden jump in loudness. mpv_daemon.sh
        reads the same key, so a restart keeps the level of whichever speaker it is.
        """
        if str(self._cfg_get("audio.speaker_volume_mac") or "") == mac:
            current = self.mpv.get_property_safe("volume") if self.mpv.available() else None
            if current is not None and current < 100:
                self._full_software_level()
            return False
        # A freshly created transport can report 0 before the speaker says
        # anything (Go 3 after a profile reconnect); taking over at 0 would play
        # silent until the slider moved. Below a few %, start from the safe level.
        target = SAFE_SPEAKER_LEVEL if hw < 5 else min(hw, SAFE_SPEAKER_LEVEL)
        result = self._write_speaker_volume(mac, target)
        if result == "ignored":
            self._use_software_volume(mac)
        elif result == "ok":
            self._cfg_set("audio.volume", str(target))
            self._last_hw_volume = (mac, target)
            self._cfg_set("audio.speaker_volume_mac", mac)
            self._full_software_level()
            return True
        return False

    def set_volume_follow(self, enabled: bool) -> dict:
        with self._volume_lock:
            self._cfg_set("audio.volume_follow_hardware", "true" if enabled else "false")
            self._volume_ignored_by = None
            if not enabled:
                mac = self._cfg_get("bluetooth.primary_mac")
                took_over = bool(mac) and str(self._cfg_get("audio.speaker_volume_mac") or "") == mac
                self._cfg_set("audio.speaker_volume_mac", "")
                hw = read_hw_volume(mac) if took_over else None
                if hw is not None and self.mpv.available():
                    # Leaving the speaker where sync put it (20 % on takeover) capped
                    # the slider: mpv was already at 100. Move the level into mpv
                    # first, so nothing gets louder, then give the speaker full range.
                    self.mpv.set_volume(hw)
                    self._cfg_set("audio.volume", str(hw))
                    if set_hw_volume(mac, 100):
                        self._last_hw_volume = (mac, 100)
                    return self.status()
                current = self.mpv.get_property_safe("volume") if self.mpv.available() else None
                if current is not None:
                    self._cfg_set("audio.volume", str(int(current)))
        return self.status()

    def sync_hw_volume_if_enabled(self) -> None:
        # Speaker buttons move the slider. A speaker that turned out to ignore volume
        # commands keeps the old arrangement: buttons adopted into software, and only
        # on an actual change, since such a speaker can report a fixed value (a static
        # one must never override the slider).
        if not self._volume_follow_enabled():
            return
        # This is the status path: never wait out a slider write's read-back.
        if not self._volume_lock.acquire(blocking=False):
            return
        try:
            mac = self._cfg_get("bluetooth.primary_mac")
            hw = read_hw_volume(mac)
            if hw is None:
                return
            if self._volume_ignored_by not in (None, mac):
                self._volume_ignored_by = None
            # A takeover just changed the level, so this reading is out of date.
            if self._volume_ignored_by != mac and self._enter_speaker_volume(mac, hw):
                return
            ignored = self._volume_ignored_by == mac
            last_mac, last_hw = getattr(self, "_last_hw_volume", (None, None))
            self._last_hw_volume = (mac, hw)
            if last_mac != mac or last_hw is None or hw == last_hw:
                return
            current = int(self._cfg_get("audio.volume") or 80)
            if abs(hw - current) < 2:
                return
            self._cfg_set("audio.volume", str(hw))
            if ignored and self.mpv.available():
                self.mpv.set_volume(hw)
        finally:
            self._volume_lock.release()

    def scan_usb(self) -> list[dict]:
        tracks: list[dict] = []
        if not self.usb_mount.is_dir():
            self._save_usb_cache(tracks)
            return tracks
        for path in sorted(self.usb_mount.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            tracks.append(
                {
                    "path": str(path),
                    "name": display(path.name),
                    "relative": str(path.relative_to(self.usb_mount)),
                }
            )
        self._save_usb_cache(tracks)
        return tracks

    def list_usb(self) -> list[dict]:
        if self.usb_cache_file.exists():
            return json.loads(self.usb_cache_file.read_text()).get("tracks") or []
        return self.scan_usb()

    def saved_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def resume_last(self) -> dict | None:
        # A saved "radio" falls through to None on purpose: RadioService brings
        # back a session that was interrupted, and resuming the last online
        # stream instead would swap the listener's source behind their back.
        saved = self.saved_state()
        source = saved.get("source")
        if source == "stream" and saved.get("url"):
            return self.play_url(saved["url"], meta=saved.get("meta") or {})
        if source == "link" and saved.get("url"):
            return self.play_link(saved["url"], meta=saved.get("meta") or {})
        if source == "link_playlist" and self.links:
            meta = saved.get("meta") or {}
            playlist = self.links.get_playlist(meta.get("playlist_id", ""))
            if playlist:
                return self.play_link_playlist(
                    self.links.playlist_links(playlist["id"]),
                    meta,
                    shuffle=playlist["shuffle"],
                    loop=playlist["loop"],
                )
        if source == "usb" and saved.get("path"):
            path = Path(saved["path"])
            if path.is_file():
                return self.play_file(str(path))
        return None

    def status(self) -> dict:
        mpv_status = self.mpv.status() if self.mpv.available() else {"running": False, "idle": True}
        saved = self.saved_state()
        follow = self._volume_follow_enabled()
        mac = self._cfg_get("bluetooth.primary_mac")
        # Not on FM: radio.py pins a 256 KB back buffer for the tuner's raw PCM
        # (tiny packets cost ~3x their size; the rewind sizing would take that
        # to the 96 MB cap). Radio is live but has no rewind.
        if mpv_status.get("running") and not mpv_status.get("idle") and not self._on_radio_fifo(mpv_status):
            self.apply_rewind(mpv_status)
        return {
            "mpv": mpv_status,
            "saved": saved,
            "eq": self.eq.active_preset(),
            "volume_config": int(self._cfg_get("audio.volume") or 80),
            "volume_follow_hardware": follow,
            "hw_volume": read_hw_volume(mac) if follow else None,
            "speaker_ignores_volume": follow and self._volume_ignored_by == mac,
            # False: connected, but the speaker has no remote volume (use mpv's).
            "speaker_remote_volume": speaker_has_remote_volume(mac) if follow else None,
            "usb_mount": str(self.usb_mount),
            "usb_mounted": self._usb_is_mounted(),
            "live": bool(mpv_status.get("running")) and not mpv_status.get("idle") and self._is_live(saved),
            "rewind_minutes": self.rewind_minutes(),
            "live_lead_s": LIVE_LEAD_S,
            "recording": self.recorder.status() if self.recorder else {"active": False},
            **self.rewind_info(),
        }

    def _usb_is_mounted(self) -> bool:
        # Not `findmnt --target`: it answers with the mount *containing* the
        # path, so an empty /media/usb on the SD card read as mounted (rc 0, the
        # root filesystem) — and it forked a process on every 1 Hz status.
        return os.path.ismount(self.usb_mount)

    def _volume_follow_enabled(self) -> bool:
        return str(self._cfg_get("audio.volume_follow_hardware") or "false").lower() in (
            "true",
            "1",
            "yes",
        )

    def _resolve_usb_path(self, path: str) -> Path:
        file_path = Path(path).expanduser()
        if not file_path.is_absolute():
            file_path = (self.usb_mount / file_path).resolve()
        else:
            file_path = file_path.resolve()
        # Allow the USB mount and any network drive (mounted under /media/net).
        roots = [self.usb_mount.resolve(), Path("/media/net")]
        if not any(file_path == r or file_path.is_relative_to(r) for r in roots):
            allowed = ", ".join(str(r) for r in roots)
            raise ValueError(f"path must be under a media mount ({allowed})")
        if not file_path.is_file():
            raise ValueError(f"file not found: {path}")
        return file_path

    def _ensure_mpv(self) -> None:
        """Guarantee a usable mpv IPC socket, repairing it when possible.

        mpv_daemon.sh exits when no Bluetooth sink was up at its start and
        systemd only retries on a ~2 min cycle, so the socket is routinely
        missing long after a speaker connected. This used to raise
        immediately — which also shadowed ensure_playback_ready() on the play
        path, the very routine that knows how to fix it.
        """
        if self.mpv.available():
            return
        mac = str(self._cfg_get("bluetooth.primary_mac") or "").strip()
        if not mac:
            raise MpvError("no Bluetooth speaker set — pair one first")
        # Restarting without a link just burns the timeout: mpv would wait for
        # a sink and exit again. Report the real cause instead.
        if not bt_connected(mac):
            raise MpvError(
                f"no Bluetooth speaker connected ({mac}) — connect one, then play"
            )
        if not restart_mpv_service(self.mpv):
            raise MpvError(
                "mpv IPC socket missing — audio-player-mpv.service failed to start"
            )

    def _write_state(self, **fields) -> dict:
        state = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **fields,
        }
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # Replaced, not rewritten in place: a power cut mid-write left an empty
        # file, and resume-on-boot with it.
        tmp = self.state_file.with_suffix(".json.part")
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        tmp.replace(self.state_file)
        return self.status()

    def _save_usb_cache(self, tracks: list[dict]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.usb_cache_file.write_text(
            json.dumps({"tracks": tracks, "updated_at": datetime.now(timezone.utc).isoformat()}, indent=2)
            + "\n"
        )
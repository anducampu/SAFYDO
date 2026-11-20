"""Saved web links — YouTube, SoundCloud, Mixcloud, Vimeo, Dailymotion, Rumble.

Playback is mpv's own ytdl_hook (see scripts/mpv_daemon.sh: --ytdl-format and
ytdl_path); this module only keeps the list and reads a link's title/duration
once, when it is added, so the list renders without calling yt-dlp again.

The list lives in the state dir (not config/) because it is user data that a
deploy must never overwrite, and it is written only when a link is added or
removed — a few writes a week, not a steady SD-card load.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

# host suffix → platform id. Only these are accepted: yt-dlp's generic
# extractor would take any page, but on a Pi it is slow and usually wrong,
# and the radio list already covers plain stream URLs.
PLATFORMS: dict[str, str] = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "soundcloud.com": "soundcloud",
    "mixcloud.com": "mixcloud",
    "vimeo.com": "vimeo",
    "dailymotion.com": "dailymotion",
    "dai.ly": "dailymotion",
    "rumble.com": "rumble",
}

PLATFORM_LABELS = {
    "youtube": "YouTube",
    "soundcloud": "SoundCloud",
    "mixcloud": "Mixcloud",
    "vimeo": "Vimeo",
    "dailymotion": "Dailymotion",
    "rumble": "Rumble",
}

# A Pi Zero 2W needs several seconds just to import yt-dlp; YouTube adds a JS
# challenge on top. Past this the link is treated as unreachable.
PROBE_TIMEOUT_S = 60
YTDL_CANDIDATES = ("/usr/local/bin/yt-dlp", "/usr/bin/yt-dlp")
COOKIE_MAX_BYTES = 1_000_000
# Sites whose API refuses yt-dlp's plain HTTP client (TLS fingerprinting):
# Rumble's embed JSON answers 403 unless the request looks like a browser.
# Needs curl_cffi, which the /opt/yt-dlp venv from install.sh provides.
IMPERSONATE = {"rumble": "chrome"}
# yt-dlp rewrites its cookie jar on exit. Hand it a copy on tmpfs so the
# uploaded file stays as the user gave it and plays cost no SD-card writes.
COOKIE_RUN_DIR = Path("/run/audio-player/cookies")


class LinkError(ValueError):
    """Bad or unplayable link — surfaced to the UI as a 400."""


def platform_of(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    for suffix, platform in PLATFORMS.items():
        if host == suffix or host.endswith("." + suffix):
            return platform
    return None


def ytdl_path() -> str | None:
    for candidate in YTDL_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("yt-dlp")


class LinksService:
    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "links.json"
        self.playlists_path = state_dir / "link_playlists.json"
        self._lock = threading.Lock()
        self._version: tuple[float, str | None] | None = None
        self.cookie_dir = state_dir / "cookies"

    # ── account cookies (Netscape cookies.txt, one file per platform) ──
    def _cookie_file(self, platform: str) -> Path:
        if platform not in PLATFORM_LABELS:
            raise LinkError(f"unknown platform: {platform}")
        return self.cookie_dir / f"{platform}.txt"

    def cookies_status(self) -> dict[str, dict]:
        # Never the content: these are live account sessions.
        out = {}
        for platform in PLATFORM_LABELS:
            path = self.cookie_dir / f"{platform}.txt"
            if path.is_file():
                out[platform] = {"saved_at": int(path.stat().st_mtime)}
        return out

    def set_cookies(self, platform: str, content: str) -> dict:
        path = self._cookie_file(platform)
        if len(content.encode()) > COOKIE_MAX_BYTES:
            raise LinkError("Cookie file is too large (max 1 MB)")
        if not _cookie_domains(content):
            raise LinkError("Not a cookies.txt file — export it in Netscape format")
        wanted = [suffix for suffix, p in PLATFORMS.items() if p == platform]
        # A browser export usually holds every site's cookies. Keep only this
        # platform's: nothing else has any business sitting on the Pi.
        content = _filter_cookies(content, wanted)
        if not content:
            raise LinkError(f"No {PLATFORM_LABELS[platform]} cookies in this file")
        self.cookie_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(content if content.endswith("\n") else content + "\n")
        os.replace(tmp, path)
        return self.cookies_status()

    def delete_cookies(self, platform: str) -> dict:
        path = self._cookie_file(platform)
        path.unlink(missing_ok=True)
        (COOKIE_RUN_DIR / path.name).unlink(missing_ok=True)
        return self.cookies_status()

    def cookies_for(self, url: str) -> str | None:
        """Path of a fresh tmpfs copy of this platform's cookies, or None."""
        platform = platform_of(url)
        if not platform:
            return None
        src = self.cookie_dir / f"{platform}.txt"
        if not src.is_file():
            return None
        COOKIE_RUN_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        dst = COOKIE_RUN_DIR / src.name
        # Replaced, not truncated in place: loading a playlist refreshes this
        # copy once per entry while yt-dlp may be reading it for the entry
        # that already plays, and a half-written jar loses the login.
        tmp = dst.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(src.read_text())
        os.replace(tmp, dst)
        return str(dst)

    # ── store ──────────────────────────────────────────────────────────
    def list(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def get(self, link_id: str) -> dict | None:
        return next((l for l in self.list() if l.get("id") == link_id), None)

    def _write(self, links: list[dict]) -> None:
        self._write_json(self.path, links)

    @staticmethod
    def _write_json(path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp, path)  # atomic: a power cut leaves old or new, never half

    def add(self, url: str) -> dict:
        url = url.strip()
        if urlparse(url).scheme not in ("http", "https"):
            raise LinkError("Paste a full link starting with https://")
        platform = platform_of(url)
        if not platform:
            raise LinkError(
                "Unsupported site — use YouTube, SoundCloud, Mixcloud, Vimeo, Dailymotion or Rumble"
            )
        with self._lock:
            existing = next((l for l in self.list() if l.get("url") == url), None)
        if existing:
            return existing
        info = self.probe(url)  # outside the lock: it takes seconds
        link = {
            "id": uuid.uuid4().hex[:12],
            "url": url,
            "platform": platform,
            "title": info.get("title") or url,
            "uploader": info.get("uploader") or info.get("channel") or "",
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail") or _first_thumb(info),
            "is_live": info.get("live_status") == "is_live" or bool(info.get("is_live")),
            "playlist_count": (
                info.get("playlist_count") or len(info.get("entries") or [])
                if info.get("_type") == "playlist"
                else None
            ),
            "added_at": int(time.time()),
        }
        with self._lock:
            links = self.list()
            # Again under the lock: the probe took seconds, and a second add of
            # the same URL (a double tap) would otherwise save it twice.
            existing = next((l for l in links if l.get("url") == url), None)
            if existing:
                return existing
            links.insert(0, link)
            self._write(links)
        return link

    def delete(self, link_id: str) -> bool:
        with self._lock:
            links = self.list()
            kept = [l for l in links if l.get("id") != link_id]
            if len(kept) == len(links):
                return False
            self._write(kept)
            # A deleted link leaves every playlist it was in.
            playlists = self.playlists()
            if any(link_id in p["link_ids"] for p in playlists):
                for p in playlists:
                    p["link_ids"] = [i for i in p["link_ids"] if i != link_id]
                self._write_json(self.playlists_path, playlists)
        return True

    # ── playlists (ordered link ids; no size limit) ────────────────────
    def playlists(self) -> list[dict]:
        try:
            data = json.loads(self.playlists_path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        data = [p for p in data if isinstance(p, dict)]
        for p in data:  # tolerate hand edits
            p.setdefault("link_ids", [])
            p.setdefault("shuffle", False)
            p.setdefault("loop", False)
        return data

    def get_playlist(self, playlist_id: str) -> dict | None:
        return next((p for p in self.playlists() if p.get("id") == playlist_id), None)

    def create_playlist(self, name: str) -> dict:
        name = name.strip()[:80]
        if not name:
            raise LinkError("Give the playlist a name")
        playlist = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "link_ids": [],
            "shuffle": False,
            "loop": False,
            "created_at": int(time.time()),
        }
        with self._lock:
            playlists = self.playlists()
            playlists.append(playlist)
            self._write_json(self.playlists_path, playlists)
        return playlist

    def update_playlist(
        self,
        playlist_id: str,
        *,
        name: str | None = None,
        link_ids: list[str] | None = None,
        shuffle: bool | None = None,
        loop: bool | None = None,
    ) -> dict:
        with self._lock:
            playlists = self.playlists()
            playlist = next((p for p in playlists if p.get("id") == playlist_id), None)
            if not playlist:
                raise KeyError(playlist_id)
            if name is not None:
                if not name.strip():
                    raise LinkError("Give the playlist a name")
                playlist["name"] = name.strip()[:80]
            if link_ids is not None:
                known = {l.get("id") for l in self.list()}
                unknown = [i for i in link_ids if i not in known]
                if unknown:
                    raise LinkError(f"Unknown link: {unknown[0]}")
                # Order is the list order; a link may appear only once.
                playlist["link_ids"] = list(dict.fromkeys(link_ids))
            if shuffle is not None:
                playlist["shuffle"] = bool(shuffle)
            if loop is not None:
                playlist["loop"] = bool(loop)
            self._write_json(self.playlists_path, playlists)
        return playlist

    def delete_playlist(self, playlist_id: str) -> bool:
        with self._lock:
            playlists = self.playlists()
            kept = [p for p in playlists if p.get("id") != playlist_id]
            if len(kept) == len(playlists):
                return False
            self._write_json(self.playlists_path, kept)
        return True

    def playlist_links(self, playlist_id: str) -> list[dict]:
        playlist = self.get_playlist(playlist_id)
        if not playlist:
            raise KeyError(playlist_id)
        by_id = {l.get("id"): l for l in self.list()}
        return [by_id[i] for i in playlist["link_ids"] if i in by_id]

    # ── yt-dlp ─────────────────────────────────────────────────────────
    def probe(self, url: str) -> dict:
        exe = ytdl_path()
        if not exe:
            raise LinkError("yt-dlp is not installed on this board")
        cmd = [exe, "-J", "--flat-playlist", "--no-warnings", "--skip-download"]
        cookies = self.cookies_for(url)
        if cookies:
            cmd += ["--cookies", cookies]
        target = IMPERSONATE.get(platform_of(url) or "")
        if target:
            cmd += ["--impersonate", target]
        cmd += ["--", url]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise LinkError(f"The site did not answer within {PROBE_TIMEOUT_S} s") from exc
        if proc.returncode != 0:
            raise LinkError(_ytdl_reason(proc.stderr) or "yt-dlp could not read this link")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise LinkError("yt-dlp returned no usable data for this link") from exc

    def version(self) -> str | None:
        # Cached for 10 min: the UI reads this on every panel mount, and each
        # call costs a Python start-up on the Pi.
        now = time.monotonic()
        if self._version and now - self._version[0] < 600:
            return self._version[1]
        exe = ytdl_path()
        ver = None
        if exe:
            try:
                ver = subprocess.run(
                    [exe, "--version"], capture_output=True, text=True, errors="replace", timeout=20
                ).stdout.strip() or None
            except (OSError, subprocess.TimeoutExpired):
                ver = None
        self._version = (now, ver)
        return ver


def _cookie_domains(content: str) -> set[str]:
    domains = set()
    for line in content.splitlines():
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) == 7:
            domains.add(fields[0].lstrip(".").lower())
    return domains


def _filter_cookies(content: str, suffixes: list[str]) -> str:
    """Netscape cookie lines whose domain belongs to one of `suffixes`,
    under a fresh header; "" when none match."""
    kept = []
    for line in content.splitlines():
        body = line[len("#HttpOnly_"):] if line.startswith("#HttpOnly_") else line
        if not body.strip() or (body.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        fields = body.split("\t")
        if len(fields) != 7:
            continue
        domain = fields[0].lstrip(".").lower()
        if any(domain == s or domain.endswith("." + s) for s in suffixes):
            kept.append(line)
    if not kept:
        return ""
    return "# Netscape HTTP Cookie File\n" + "\n".join(kept) + "\n"


def _first_thumb(info: dict) -> str | None:
    thumbs = info.get("thumbnails") or []
    for t in reversed(thumbs):  # yt-dlp orders them worst → best
        if isinstance(t, dict) and t.get("url"):
            return t["url"]
    return None


def _ytdl_reason(stderr: str) -> str:
    lines = [l.strip() for l in (stderr or "").splitlines() if "ERROR" in l]
    if not lines:
        return ""
    msg = lines[-1].split("ERROR:", 1)[-1].strip()
    return msg[:240]

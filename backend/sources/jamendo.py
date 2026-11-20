"""Jamendo — Creative-Commons music, free to stream (needs a free client_id).

Jamendo's v3 API needs an app `client_id` (free at developer.jamendo.com). Set
it in Settings → Sources (stored as sources.jamendo.client_id). Tracks are CC
licensed and the API returns a streamable MP3 URL that mpv plays directly.
"""

from __future__ import annotations

import urllib.parse

from .base import SourceProvider

BASE = "https://api.jamendo.com/v3.0"
GENRES = [
    "pop", "rock", "electronic", "hiphop", "jazz", "classical", "lounge",
    "ambient", "folk", "metal", "reggae", "soundtrack", "world", "chillout",
]


class Jamendo(SourceProvider):
    id = "jamendo"
    label = "Jamendo (CC)"
    needs_config = True

    def _client_id(self) -> str:
        return str(self.cfg.get("client_id") or "").strip()

    def available(self) -> bool:
        return bool(self._client_id())

    def genres(self) -> list[str]:
        return [g.upper() for g in GENRES]

    def browse(self, genre=None, query=None, page=0, limit=50) -> list[dict]:
        cid = self._client_id()
        if not cid:
            return []
        params = {
            "client_id": cid,
            "format": "json",
            "limit": str(limit),
            "offset": str(max(0, page) * limit),
            "audioformat": "mp32",
            "include": "musicinfo",
            "order": "popularity_total",
        }
        if query:
            params["namesearch"] = query
        if genre:
            params["tags"] = genre.lower()
        try:
            data = self._get_json(f"{BASE}/tracks/?" + urllib.parse.urlencode(params))
        except Exception:
            return []
        out: list[dict] = []
        for t in (data or {}).get("results", []):
            url = (t.get("audio") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            artist = t.get("artist_name") or ""
            name = t.get("name") or "Untitled"
            out.append(
                {
                    "id": t.get("id") or url,
                    "name": (f"{artist} — {name}" if artist else name)[:80],
                    "url": url,
                    "genre": (genre or "CC").upper(),
                    "homepage": t.get("shareurl") or None,
                    "source": self.id,
                    # A finished MP3, not a broadcast: seekable end to end, and
                    # not something to record (stream-record would start late).
                    "live": False,
                }
            )
        return out

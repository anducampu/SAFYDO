"""Source-plugin interface for station/track directories.

A *source* is a read-only directory that resolves to playable http(s) URLs —
mpv plays any URL, so a provider only has to fetch + normalize. Providers do
**HTTP GET only**: no player access, no filesystem, no code execution. That
makes them safe to add/develop by others and cheap to run (on-demand fetches
with short timeouts, no background work).

To add a source:
  1. subclass SourceProvider, set `id` + `label`, implement `genres()` and
     `browse()` returning normalized dicts (use `_get_json` / `_get_json_any`);
  2. register the class in `backend/sources/__init__.py` PROVIDERS.

Normalized item shape (only name+url are required):
  {"id", "name", "url", "genre", "bitrate"?, "homepage"?, "country"?, "source",
   "live"?}   # live: a broadcast (True, the default) or a finite file (False)
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

USER_AGENT = "SAFYDO/1.0 (rpi-audio-player; +https://safydo.local)"


class SourceProvider:
    id: str = ""
    label: str = ""
    # True if the plugin needs configuration (e.g. an API key) before it works.
    needs_config: bool = False

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or {}

    # Providers whose config is incomplete (e.g. missing key) return False so the
    # UI can show "needs setup" instead of failing on every request.
    def available(self) -> bool:
        return True

    def genres(self) -> list[str]:  # pragma: no cover - interface
        return []

    def browse(
        self, genre: str | None = None, query: str | None = None, page: int = 0, limit: int = 50
    ) -> list[dict]:  # pragma: no cover - interface
        return []

    # ---- helpers ------------------------------------------------------------
    @staticmethod
    def _quote(s: str) -> str:
        return urllib.parse.quote(s, safe="")

    @staticmethod
    def _get_json(url: str, timeout: int = 8, headers: dict | None = None):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)

    @classmethod
    def _get_json_any(cls, bases: list[str], path: str, timeout: int = 8):
        """Try several mirror hosts in order until one answers (radio-browser)."""
        last: Exception | None = None
        for base in bases:
            try:
                return cls._get_json(base.rstrip("/") + path, timeout=timeout)
            except Exception as exc:  # try the next mirror
                last = exc
        if last:
            raise last
        return None

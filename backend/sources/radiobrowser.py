"""radio-browser.info — a free, community internet-radio directory (no API key).

Aggregates thousands of public Icecast/Shoutcast streams; browse by tag/genre,
search, or top-by-popularity. Public streams that opted into the directory, so
listing/playing them is the intended use. Several mirrors exist — we try a few.
"""

from __future__ import annotations

from .base import SourceProvider

MIRRORS = [
    "https://de1.api.radio-browser.info",
    "https://de2.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
]
COMMON = "hidebroken=true&order=clickcount&reverse=true"


class RadioBrowser(SourceProvider):
    id = "radiobrowser"
    label = "Radio Browser"

    def genres(self) -> list[str]:
        try:
            data = self._get_json_any(
                MIRRORS, "/json/tags?order=stationcount&reverse=true&hidebroken=true&limit=36"
            )
        except Exception:
            return []
        return [t["name"] for t in (data or []) if t.get("name")]

    def browse(self, genre=None, query=None, page=0, limit=50) -> list[dict]:
        offset = max(0, page) * limit
        if query:
            path = f"/json/stations/search?name={self._quote(query)}&{COMMON}&limit={limit}&offset={offset}"
        elif genre:
            path = f"/json/stations/bytagexact/{self._quote(genre)}?{COMMON}&limit={limit}&offset={offset}"
        else:
            path = f"/json/stations/topclick/{limit}"
        try:
            data = self._get_json_any(MIRRORS, path)
        except Exception:
            return []
        out: list[dict] = []
        for s in data or []:
            url = (s.get("url_resolved") or s.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            tag = (s.get("tags", "").split(",")[0] or "").strip().upper()
            out.append(
                {
                    "id": s.get("stationuuid") or url,
                    "name": (s.get("name") or "Unknown").strip()[:80],
                    "url": url,
                    "genre": tag or "RADIO",
                    "bitrate": s.get("bitrate") or None,
                    "homepage": s.get("homepage") or None,
                    "favicon": s.get("favicon") or None,
                    "country": s.get("countrycode") or None,
                    "source": self.id,
                    "live": True,
                }
            )
        return out

#!/usr/bin/env python3
"""Import stations.json → config/streams.json for the player."""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path

INSTALL_DIR = Path("/opt/rpi-audio-player")


def slugify(title: str) -> str:
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "station"


def load_stations(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError(f"expected JSON array in {path}")
    return data


def convert(stations: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for row in stations:
        if row.get("disable"):
            continue
        title = (row.get("title") or "").strip()
        url = (row.get("url") or "").strip()
        if not title or not url:
            continue
        base_id = slugify(title)
        sid = base_id
        n = 2
        while sid in seen:
            sid = f"{base_id}-{n}"
            n += 1
        seen.add(sid)
        genre = (row.get("genre") or "Radio").strip()
        out.append(
            {
                "id": sid,
                "name": title,
                "url": url,
                "genre": genre,
                "category": "Radio",
                "favorite": bool(row.get("favorite")),
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    install = args.install_dir
    candidates = [install / "stations.json", install / "config" / "stations.json"]
    src = next((p for p in candidates if p.exists()), None)
    if not src:
        print("stations.json not found", flush=True)
        return 1

    streams = convert(load_stations(src))
    out_path = args.out or (install / "config" / "streams.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"streams": streams}, indent=2) + "\n")
    print(f"Wrote {len(streams)} streams to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""Text that came from outside — stations, files, nearby devices — never breaks the box.

Where odd text gets in, and what each boundary does:

* **mpv IPC** decodes replies with ``surrogateescape`` (backend/services/mpv.py):
  a station's Latin-1 ICY title (Antenne Kärnten sends byte 0xe4) or a
  non-UTF-8 file path survives as lone surrogates instead of raising.
* **Subprocess output** (bluetoothctl, nmcli, ...) is decoded with
  ``errors="replace"``: a nearby Bluetooth device can advertise any bytes.
* **Display text** (song titles, station names, file names in the UI) goes
  through :func:`display` — bytes that are not UTF-8 are read as Windows-1252
  (what such stations and old FAT sticks almost always mean), control
  characters are dropped, and absurd lengths are cut.
* **API output** uses :class:`SafeJSONResponse`: if a string cannot be encoded
  as UTF-8 (a raw byte kept for a file path), the body falls back to ASCII
  escapes, so the client gets valid JSON and can send the value back intact.
* **Playing a file** whose name is not UTF-8: mpv's JSON IPC cannot carry raw
  bytes, so :func:`mpv_path` hands it an ASCII symlink under /run instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

from starlette.responses import JSONResponse

# C0 controls except tab/newline, DEL, C1 controls, and the bidi overrides
# that can make a title render backwards over the rest of the line.
_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")
_SPACE_RE = re.compile(r"\s+")

DISPLAY_MAX = 300

LINK_DIR = Path("/run/audio-player/paths")


def _repair(text: str) -> str:
    """Lone surrogates (raw bytes kept by surrogateescape) back to letters."""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        pass
    raw = text.encode("utf-8", "surrogateescape")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


def display(value: Any, max_len: int = DISPLAY_MAX) -> str:
    """Safe, tidy text for the UI. Never raises; None becomes ''."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = _repair(text)
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL_RE.sub("", text)
    text = _SPACE_RE.sub(" ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def json_bytes(content: Any) -> bytes:
    """UTF-8 JSON, or ASCII-escaped JSON when a string holds raw bytes."""
    try:
        return json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(content, ensure_ascii=True, separators=(",", ":")).encode("ascii")


class SafeJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json_bytes(content)


def mpv_path(path: str) -> str:
    """A path mpv can be given over JSON IPC.

    UTF-8 paths pass through. One holding raw bytes (a stick written by an old
    system) gets a stable ASCII symlink in a tmpfs directory, keeping the
    extension so mpv still picks the right demuxer.
    """
    try:
        path.encode("utf-8")
        return path
    except UnicodeEncodeError:
        pass
    raw = os.fsencode(path)
    LINK_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(path).suffix.encode("ascii", "ignore").decode() if Path(path).suffix else ""
    link = LINK_DIR / (hashlib.sha1(raw).hexdigest()[:16] + suffix)
    try:
        if os.readlink(link) != path:
            link.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        link.unlink(missing_ok=True)
    if not link.is_symlink():
        os.symlink(raw, os.fsencode(link))
    return str(link)

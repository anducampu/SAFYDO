"""Find a station's stream from its website, and find a station's logo.

Ported from the StreamJockey Android app (StreamProber + LogoFetcher), stdlib
only — no Pillow on the board, so image sizes come from the file headers.

Stream discovery (``probe``), best first:
  direct sniff (the input already is a stream / playlist) → the page's HTML
  (``<audio>``/``<source>``, audio-extension URLs, quoted root-relative paths,
  keyword/port hints) → the page's script bundles when the HTML is thin
  (JWPlayer-style configs) → one hop into the site's player pages (iframes,
  ``/player/``, ``/live``, ``/asculta``…) → the Radio Browser directory entry
  whose homepage is this site. Every candidate is verified by actually opening
  it, so only URLs that answer like audio come back.

Logo search (``find_logo``), best first:
  a known hint (a directory favicon) → the Radio Browser entry's favicon
  (matched by stream URL, then by name) → the homepage's apple-touch-icon,
  web-manifest icons, large ``rel=icon``, og:image → /apple-touch-icon.png →
  small icons → /favicon.ico. The first image whose short side is ≥ 128 px
  wins; otherwise the largest ≥ 48 px. SVG is refused (it would be served from
  the UI's own origin), banners wider than 4:1 too.

The backend runs as root on the LAN, so everything fetched on the strength of
third-party data (scraped links, icy-url, directory entries) must resolve to a
public address — every redirect hop included. Only the URL the user typed
themselves may point into the LAN (a local Icecast is legitimate).
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

LOGO_DIR = Path("/var/lib/audio-player/logos")
LOGO_NAME_RE = re.compile(r"^[0-9a-f]{16}\.(png|jpg|gif|webp|ico|svg)$")
MAX_SVG_BYTES = 1024 * 1024
# What a sanitised SVG served from the UI's own origin may do: draw, nothing else.
SVG_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 SAFYDO/1.0"
)
APP_UA = "SAFYDO/1.0 (rpi-audio-player)"
RB_HOSTS = (
    "https://all.api.radio-browser.info",
    "https://de1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
)

TIMEOUT = 7
PAGE_BYTES = 600_000
MAX_IMAGE_BYTES = 3 * 1024 * 1024
CONCURRENCY = 4

# Verify budget (StreamJockey/server.mjs parity).
HINT_WEIGHT = 2
MIN_STRONG_TO_SKIP_SCRIPTS = 3
MAX_STRONG = 12
MAX_HINTS = 6
MAX_VERIFY = 14
MAX_SCRIPTS = 6
MAX_CRAWL_PAGES = 4
MAX_PLAYER_SEGMENT = 24

GOOD_SIDE = 128
MIN_SIDE = 48
MAX_ASPECT = 4
MAX_IMAGE_TRIES = 8

AUDIO_EXT = "mp3|aac|aacp|ogg|opus|flac|m3u8|m3u|pls"
ASSET_EXT_ALT = "png|jpe?g|gif|svg|webp|ico|css|woff2?|ttf|otf|mp4|webm|pdf|zip|xml|json"
JUNK_HOST = (
    r"google|gstatic|facebook|twitter|instagram|youtube|tiktok|doubleclick|cookie|consent"
    r"|analytics|recaptcha|cdn-cgi|fonts\.|schema\.org|w3\.org"
)

TITLE_RE = re.compile(r"<title[^>]*>([^<]{1,120})</title>", re.I)
SCRIPT_SRC_RE = re.compile(r"<script[^>]+src=[\"']([^\"']+)[\"']", re.I)
AUDIO_TAG_RE = re.compile(r"<(?:audio|source)[^>]+src=[\"']([^\"']+)[\"']", re.I)
AUDIO_URL_RE = re.compile(
    rf"(?:https?:)?//[^\s\"'<>\\]+?\.(?:{AUDIO_EXT})(?:\?[^\s\"'<>\\]*)?", re.I
)
RELATIVE_AUDIO_RE = re.compile(
    rf"[\"'](/[^\"'\s<>\\]*\.(?:{AUDIO_EXT})(?:\?[^\"'\s<>\\]*)?)[\"']", re.I
)
HINT_URL_RE = re.compile(
    r"(?:https?:)?//[^\s\"'<>\\]*(?:stream|listen|live|radio|icecast|shoutcast|:[0-9]{4,5}/)[^\s\"'<>\\]*",
    re.I,
)
LINK_HREF_RE = re.compile(r"<a\s[^>]*href=[\"']([^\"']+)[\"']", re.I)
IFRAME_SRC_RE = re.compile(r"<iframe\s[^>]*src=[\"']([^\"']+)[\"']", re.I)
WINDOW_OPEN_RE = re.compile(r"window\.open\(\s*[\"']([^\"']+)[\"']", re.I)
PLAYER_SEGMENT_RE = re.compile(
    r"player|live|listen|asculta|ascultă|stream|onair|on-air|webradio|radio", re.I
)
GUESSED_PLAYER_PATHS = ("/player/", "/live/", "/listen/")
AUDIO_EXT_URL_RE = re.compile(rf"\.(?:{AUDIO_EXT})(\?|$)", re.I)
ASSET_EXT_RE = re.compile(rf"\.(js|{ASSET_EXT_ALT})([?#]|$)", re.I)
JUNK_HOSTS_RE = re.compile(JUNK_HOST, re.I)
SCRIPT_SKIP_RE = re.compile(
    rf"\.({ASSET_EXT_ALT})([?#]|$)|{JUNK_HOST}|jquery|bootstrap|tether|polyfill|wp-includes", re.I
)
TITLE_SEP_RE = re.compile(r"\s+[-|–—:•]\s+")
NAME_TAGS_RE = re.compile(r"\[[^]]*]|\([^)]*\)")

LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.I)
META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)


# ---- HTTP -------------------------------------------------------------------


def _is_public_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global


def _is_lan_ip(addr: str) -> bool:
    """Reachable for a URL the user typed: public or private LAN — never the
    board itself (loopback: the backend, mpv's helpers), link-local (cloud
    metadata, zeroconf), unspecified or multicast."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved)


def host_allowed(url: str, public_only: bool) -> bool:
    """Every address the URL's host resolves to passes (a pre-check: a DNS
    answer that changes before the connect is out of scope)."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        if not host or parts.scheme not in ("http", "https"):
            return False
        infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except (OSError, ValueError):
        return False
    ok = _is_public_ip if public_only else _is_lan_ip
    return bool(infos) and all(ok(i[4][0]) and not _is_own_address(i[4][0]) for i in infos)


def _is_own_address(addr: str) -> bool:
    """An address of this board (its LAN IP, safydo.local, its global IPv6).
    Loopback alone is not "the board": nginx answers on every interface and
    proxies /api to the backend. Binding succeeds only for a local address."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.bind((str(ip), 0))
        return True
    except OSError:
        return False


def host_is_public(url: str) -> bool:
    return host_allowed(url, True)


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Re-checks every hop when the request is public-only."""

    def __init__(self, public_only: bool) -> None:
        self.public_only = public_only

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith(("http://", "https://")):
            return None
        # Where a public server sends us is that server's choice — third-party
        # data — so it may not lead into the LAN even when the user's own URL
        # was allowed to. A LAN server may still redirect within the LAN.
        public_only = self.public_only or host_is_public(req.full_url)
        if not host_allowed(newurl, public_only):
            raise urllib.error.URLError(f"blocked redirect to a disallowed address: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _ua_for(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return APP_UA if host.endswith("radio-browser.info") else BROWSER_UA


class _Resp:
    def __init__(self, url: str, status: int, headers, body: bytes, icy: bool = False) -> None:
        self.url = url
        self.status = status
        self.headers = headers  # email.message.Message or {}
        self.body = body
        self.icy = icy  # a Shoutcast v1 "ICY 200 OK" status line

    def header(self, name: str) -> str:
        try:
            return (self.headers.get(name) or "").strip()
        except AttributeError:
            return ""

    @property
    def ctype(self) -> str:
        return self.header("Content-Type").lower().split(";", 1)[0].strip()

    def has_icy(self) -> bool:
        try:
            return self.icy or any(k.lower().startswith("icy-") for k in self.headers.keys())
        except AttributeError:
            return self.icy

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def _open(
    url: str,
    *,
    public_only: bool,
    max_bytes: int,
    headers: dict | None = None,
    body_if: Callable[[_Resp], bool] | None = None,
) -> _Resp | None:
    """GET [url]; reads at most [max_bytes] of the body, and only when
    [body_if] (given the status + headers) says it is worth reading — a live
    audio stream never ends, so its body is never touched."""
    if not host_allowed(url, public_only):
        return None
    opener = urllib.request.build_opener(_Redirects(public_only))
    req = urllib.request.Request(url, headers={"User-Agent": _ua_for(url), **(headers or {})})
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            resp = _Resp(r.geturl(), r.status, r.headers, b"")
            if max_bytes and (body_if is None or body_if(resp)):
                resp.body = r.read(max_bytes)
            return resp
    except urllib.error.HTTPError as exc:
        # Kept, not dropped: the redirects were already followed, and where
        # they ended matters even when that last page refuses us (www.rfi.ro →
        # 403 at www.rfi.fr/ro/, whose /favicon.ico is still served).
        return _Resp(exc.url or url, exc.code, exc.headers or {}, b"")
    except http.client.BadStatusLine as exc:
        if "ICY 200" in str(exc):
            return _Resp(url, 200, {}, b"", icy=True)
        return None
    except (OSError, ValueError, http.client.HTTPException):
        return None


def _fetch_text(url: str, *, public_only: bool = True, max_bytes: int = PAGE_BYTES, any_type: bool = False) -> str | None:
    def textual(r: _Resp) -> bool:
        return any_type or "html" in r.ctype or "text" in r.ctype or "javascript" in r.ctype

    r = _open(url, public_only=public_only, max_bytes=max_bytes, body_if=textual)
    if r is None or r.status >= 400 or not textual(r):
        return None
    return r.text()


def _fetch_json(url: str, max_bytes: int = 1_500_000):
    text = _fetch_text(url, max_bytes=max_bytes, any_type=True)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def release_memory() -> None:
    """Hands freed heap back to the OS. A lookup reads several 600 KB pages on
    worker threads, and glibc keeps those per-thread arenas mapped afterwards —
    a 77-station logo pass left the backend at 103 MB RSS instead of 68."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def _pmap(fn, items: list, limit: int = CONCURRENCY) -> list:
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(limit, len(items))) as pool:
        return list(pool.map(fn, items))


# ---- URL helpers --------------------------------------------------------------


def normalize(raw: str) -> str | None:
    t = (raw or "").strip()
    if not t:
        return None
    u = t if t.lower().startswith(("http://", "https://")) else f"https://{t}"
    try:
        host = urllib.parse.urlsplit(u).hostname or ""
    except ValueError:
        return None
    return u if "." in host or host == "localhost" else None


# Second-level labels that act as a public suffix under a country code
# (co.uk, com.ro). Not "any label of three letters or fewer": that read pro.fm
# and rfi.ro as suffixes, so live.pro.fm was "another site" than pro.fm.
_GENERIC_SLD = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go", "gob", "mil", "nom", "ltd", "plc", "sch"}


def site_key(host: str) -> str:
    """Registrable-ish domain: last two labels, three for co.uk / com.ro."""
    labels = host.lower().removeprefix("www.").split(".")
    n = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _GENERIC_SLD else 2
    return ".".join(labels[-n:])


def _resolve(base: str, href: str) -> str | None:
    try:
        u = urllib.parse.urljoin(base, href.strip().replace("&amp;", "&"))
    except ValueError:
        return None
    return u if u.lower().startswith(("http://", "https://")) else None


def _path_of(url: str) -> str:
    return url.split("?", 1)[0].split("#", 1)[0].lower()


def station_name_from_title(title: str) -> str | None:
    t = title.replace("&amp;", "&").replace("&#039;", "'").strip()
    for part in TITLE_SEP_RE.split(t):
        if part.strip():
            return part.strip()
    return None


def directory_query(name: str) -> str:
    q = re.sub(r"\s+", " ", NAME_TAGS_RE.sub(" ", name).strip())
    return q or name.strip()


def _name_key(name: str) -> str:
    return "".join(c for c in directory_query(name).lower() if c.isalnum())


def _url_key(url: str) -> str:
    u = url.strip().lower()
    return (u.split("://", 1)[1] if "://" in u else u).rstrip("/")


def _site_path_key(url: str) -> str | None:
    try:
        p = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return None
    path = (p.path or "").rstrip("/").lower()
    if len(path) < 3 or not p.hostname:
        return None
    return site_key(p.hostname) + path


# ---- Stream discovery --------------------------------------------------------


def _format_of(ct: str, path: str) -> str | None:
    if "mpeg" in ct and "mpegurl" not in ct:
        return "MP3"
    for key, fmt in (("aac", "AAC"), ("ogg", "OGG"), ("vorbis", "OGG"), ("opus", "OPUS"), ("flac", "FLAC")):
        if key in ct:
            return fmt
    for ext, fmt in ((".mp3", "MP3"), (".aac", "AAC"), (".aacp", "AAC"), (".ogg", "OGG"), (".opus", "OPUS"), (".flac", "FLAC")):
        if path.endswith(ext):
            return fmt
    return None


def _parse_playlist(text: str) -> str | None:
    for line in text.splitlines():
        t = line.strip()
        if not t or t.startswith(("#", "[")):
            continue
        if t.lower().startswith("file") and "=" in t:
            v = t.split("=", 1)[1].strip()
            if v.lower().startswith("http"):
                return v
        elif t.lower().startswith("http"):
            return t
    return None


def sniff(url: str, *, public_only: bool = True, depth: int = 0) -> dict | None:
    """The URL as a stream: a candidate dict if it answers like audio (or HLS,
    which mpv plays); playlists (.pls/.m3u) are followed to their first entry."""
    if depth > 2:
        return None
    path = _path_of(url)

    def is_playlist(r: _Resp) -> bool:
        ct = r.ctype
        return (path.endswith((".pls", ".m3u")) and not path.endswith(".m3u8")) or ct in (
            "audio/x-scpls",
            "audio/x-mpegurl",
        )

    r = _open(url, public_only=public_only, max_bytes=64 * 1024, headers={"Icy-MetaData": "1"}, body_if=is_playlist)
    if r is None or r.status >= 400:
        return None
    ct = r.ctype
    br_raw = r.header("icy-br").split(",", 1)[0].strip()
    bitrate = int(br_raw) if br_raw.isdigit() else None
    if bitrate and bitrate >= 10_000:
        bitrate //= 1000
    cand = {
        "url": url,
        "name": r.header("icy-name") or None,
        "genre": r.header("icy-genre") or None,
        "homepage": r.header("icy-url") or None,
        "format": None,
        "bitrate": bitrate,
        # A live mount announces itself (icy-*) or has no length; a sized file
        # without either is a jingle or a podcast episode on the same page.
        "live": r.has_icy() or not r.header("Content-Length"),
    }
    if (ct.startswith("audio/") and "mpegurl" not in ct and ct != "audio/x-scpls") or ct == "application/ogg" or r.has_icy():
        cand["format"] = _format_of(ct, path)
        return cand
    if "mpegurl" in ct or path.endswith(".m3u8"):
        if path.endswith(".m3u8") or "vnd.apple" in ct or "application/x-mpegurl" in ct:
            cand["format"] = "HLS"
            cand["live"] = True
            return cand
    if is_playlist(r) and r.body:
        target = _parse_playlist(r.text())
        if target:
            # The user's own playlist may name a LAN server; a scraped one may not.
            inner = sniff(target, public_only=public_only, depth=depth + 1)
            if inner:
                inner["name"] = inner["name"] or cand["name"]
                return inner
    return None


def _add_candidate(found: dict, base: str, raw: str, force_strong: bool = False) -> None:
    u = raw.strip().split("#", 1)[0]
    if u.startswith("//"):
        u = "https:" + u
    if not u.lower().startswith("http"):
        u = _resolve(base, u) or ""
        if not u:
            return
    if len(u) > 500 or ASSET_EXT_RE.search(u.split("?", 1)[0]):
        return
    if force_strong:
        weight = 0
    elif AUDIO_EXT_URL_RE.search(u):
        weight = 1 if ".m3u" in u.lower() else 0
    else:
        weight = HINT_WEIGHT
    if weight >= HINT_WEIGHT and JUNK_HOSTS_RE.search(u):
        return
    if u not in found or found[u] > weight:
        found[u] = weight


def extract_weighted(raw: str, base: str) -> dict[str, int]:
    text = raw.replace("\\/", "/") if "\\/" in raw else raw
    if "&amp;" in text:
        text = text.replace("&amp;", "&")
    found: dict[str, int] = {}
    for m in AUDIO_TAG_RE.finditer(text):
        _add_candidate(found, base, m.group(1), force_strong=True)
    for m in AUDIO_URL_RE.finditer(text):
        _add_candidate(found, base, m.group(0))
    for m in RELATIVE_AUDIO_RE.finditer(text):
        _add_candidate(found, base, m.group(1))
    for m in HINT_URL_RE.finditer(text):
        _add_candidate(found, base, m.group(0))
    return found


def _merge_min(into: dict, other: dict) -> None:
    for u, w in other.items():
        if u not in into or into[u] > w:
            into[u] = w


def _scan_scripts(html: str, base: str) -> dict[str, int]:
    base_host = urllib.parse.urlsplit(base).hostname
    resolved = []
    for m in SCRIPT_SRC_RE.finditer(html):
        u = _resolve(base, m.group(1))
        if u and not SCRIPT_SKIP_RE.search(u) and u not in resolved:
            resolved.append(u)
    same = [u for u in resolved if urllib.parse.urlsplit(u).hostname == base_host]
    other = [u for u in resolved if u not in same]
    merged: dict[str, int] = {}
    for found in _pmap(
        lambda su: extract_weighted(_fetch_text(su, any_type=True) or "", su), (same + other)[:MAX_SCRIPTS]
    ):
        _merge_min(merged, found)
    return merged


def _harvest(page: str, page_url: str) -> dict[str, int]:
    weighted = extract_weighted(page, page_url)
    if sum(1 for w in weighted.values() if w < HINT_WEIGHT) >= MIN_STRONG_TO_SKIP_SCRIPTS:
        return weighted
    merged = dict(weighted)
    _merge_min(merged, _scan_scripts(page, page_url))
    return merged


def _select_for_verify(weighted: dict[str, int]) -> list[str]:
    ordered = sorted(weighted.items(), key=lambda kv: kv[1])
    strong = [u for u, w in ordered if w < HINT_WEIGHT][:MAX_STRONG]
    hints = [u for u, w in ordered if w >= HINT_WEIGHT][: min(MAX_VERIFY - len(strong), MAX_HINTS)]
    return strong + hints


def _verify(urls: list[str]) -> list[dict]:
    out, seen = [], set()
    for c in _pmap(lambda u: sniff(u, public_only=True), urls):
        if c and c["url"] not in seen:
            seen.add(c["url"])
            out.append(c)
    return out


def player_page_links(html: str, page_url: str) -> list[str]:
    base = urllib.parse.urlsplit(page_url)
    if not base.hostname:
        return []
    site = site_key(base.hostname)
    self_key = base.hostname.removeprefix("www.") + (base.path or "").rstrip("/")
    scored: dict[str, int] = {}

    def consider(raw: str, iframe: bool) -> None:
        r = raw.strip().replace("&amp;", "&")
        if r.lower().startswith(("javascript:", "mailto:")):
            return
        u = _resolve(page_url, r)
        if not u:
            return
        u = u.split("#", 1)[0]
        if len(u) > 300 or ASSET_EXT_RE.search(u.split("?", 1)[0]):
            return
        p = urllib.parse.urlsplit(u)
        host = (p.hostname or "").lower()
        path = (p.path or "").lower()
        if not host or host.removeprefix("www.") + path.rstrip("/") == self_key:
            return
        segments = [s for s in path.split("/") if s]
        if iframe:
            if JUNK_HOSTS_RE.search(host):
                return
            score = 0
        elif site_key(host) != site or len(segments) > 2:
            return
        elif any(len(s) <= MAX_PLAYER_SEGMENT and PLAYER_SEGMENT_RE.search(s) for s in segments):
            score = len(segments)
        else:
            return
        scored[u] = min(scored.get(u, score), score)

    for m in IFRAME_SRC_RE.finditer(html):
        consider(m.group(1), True)
    for m in LINK_HREF_RE.finditer(html):
        consider(m.group(1), False)
    for m in WINDOW_OPEN_RE.finditer(html):
        consider(m.group(1), False)
    if not scored:
        for p in GUESSED_PLAYER_PATHS:
            scored[urllib.parse.urljoin(page_url, p)] = 9
    return [u for u, _ in sorted(scored.items(), key=lambda kv: (kv[1], len(kv[0])))][:MAX_CRAWL_PAGES]


def _crawl_player_pages(html: str, page_url: str) -> list[dict]:
    pages = player_page_links(html, page_url)
    merged: dict[str, int] = {}

    def harvest_page(p: str) -> dict:
        text = _fetch_text(p)
        return _harvest(text, p) if text else {}

    for found in _pmap(harvest_page, pages, limit=2):
        _merge_min(merged, found)
    return _verify(_select_for_verify(merged))


def best_first(streams: list[dict]) -> list[dict]:
    """Highest bitrate first, https before http; URLs that differ only by query
    collapse to the shortest form (same mount with player cruft attached)."""
    seen, uniq = set(), []
    for s in sorted(streams, key=lambda s: len(s["url"])):
        k = s["url"].split("?", 1)[0]
        if k not in seen:
            seen.add(k)
            uniq.append(s)
    return sorted(uniq, key=lambda s: (-(s.get("bitrate") or 0), not s["url"].lower().startswith("https")))


def _rb_get(path: str):
    for host in RB_HOSTS:
        data = _fetch_json(host + path)
        if isinstance(data, list):
            return data
    return None


def _rb_candidate(e: dict) -> dict:
    url = (e.get("url_resolved") or e.get("url") or "").strip()
    tag = (str(e.get("tags") or "").split(",")[0] or "").strip().upper()
    return {
        "url": url,
        "name": (e.get("name") or "").strip() or None,
        "genre": tag or None,
        "homepage": (e.get("homepage") or "").strip() or None,
        "format": (e.get("codec") or "").upper() or None,
        "bitrate": e.get("bitrate") or None,
        "logo_hint": (e.get("favicon") or "").strip() or None,
        "live": True,
    }


def _rb_by_site(url: str) -> list[dict]:
    host = (urllib.parse.urlsplit(url).hostname or "").removeprefix("www.")
    label = host.split(".", 1)[0]
    if len(label) < 3:
        return []
    data = _rb_get(
        f"/json/stations/search?name={urllib.parse.quote(label)}&limit=100&hidebroken=true&order=votes&reverse=true"
    )
    out = [
        _rb_candidate(e)
        for e in (data or [])
        if isinstance(e, dict) and host and host in str(e.get("homepage") or "").lower()
    ]
    return [c for c in out if c["url"].lower().startswith(("http://", "https://"))][:8]


def probe(user_input: str) -> dict:
    try:
        return _probe(user_input)
    finally:
        release_memory()


def _probe(user_input: str, public_only: bool = False) -> dict:
    """{"streams": [candidate, …] best first, "title": page title, "homepage",
    "error"}. A candidate: url, name, genre, homepage, format, bitrate."""
    url = normalize(user_input)
    if not url:
        return {"streams": [], "title": None, "homepage": None, "error": "Not a valid URL"}
    # public_only=False only for what the user just typed into Add station; a
    # stored homepage (directory data, icy-url) is third-party and gets the guard.
    direct = sniff(url, public_only=public_only)
    if direct:
        return {"streams": [direct], "title": None, "homepage": direct.get("homepage"), "error": None}
    # Links on the page are relative to where the redirects ended (www.rfi.ro →
    # www.rfi.fr/ro/), not to what was typed: resolving against the typed URL
    # sent relative streams, script bundles and player pages to the wrong host.
    page, final = _fetch_page(url, PAGE_BYTES, public_only=public_only)
    title = None
    verified: list[dict] = []
    if page is not None:
        m = TITLE_RE.search(page)
        title = station_name_from_title(m.group(1)) if m else None
        verified = _verify(_select_for_verify(_harvest(page, final))) or _crawl_player_pages(page, final)
    source = "page"
    if not verified:
        verified = _rb_by_site(url)
        source = "directory"
    if not verified:
        reason = "No streams found on this page" if page is not None else "The site did not answer"
        return {"streams": [], "title": title, "homepage": url, "error": reason}
    if any(c.get("live") for c in verified):
        verified = [c for c in verified if c.get("live")]
    for c in verified:
        c["name"] = c.get("name") or title
        c["homepage"] = c.get("homepage") if source == "directory" and c.get("homepage") else url
    return {"streams": best_first(verified)[:10], "title": title, "homepage": url, "error": None}


# ---- Logo search ----------------------------------------------------------------


def image_info(data: bytes) -> tuple[str, int, int] | None:
    """(ext, width, height) from the header bytes, or None if not an image we
    keep (PNG, JPEG, GIF, WebP, ICO). For an ICO the largest entry counts."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            return "png", int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "gif", int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8 ":
                return "webp", int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
            if chunk == b"VP8L":
                b = int.from_bytes(data[21:25], "little")
                return "webp", (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
            if chunk == b"VP8X":
                return "webp", int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
            return None
        if data[:4] == b"\x00\x00\x01\x00":
            count = int.from_bytes(data[4:6], "little")
            best = 0
            for i in range(min(count, 64)):
                e = data[6 + 16 * i : 22 + 16 * i]
                if len(e) < 16:
                    break
                best = max(best, e[0] or 256)
            return ("ico", best, best) if best else None
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg = int.from_bytes(data[i + 2 : i + 4], "big")
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h = int.from_bytes(data[i + 5 : i + 7], "big")
                    w = int.from_bytes(data[i + 7 : i + 9], "big")
                    return "jpg", w, h
                i += 2 + seg
            return None
    except (IndexError, ValueError):
        return None
    return None


_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
# Elements that run code, embed other documents or fetch from elsewhere.
_SVG_DROP = {"script", "foreignobject", "iframe", "embed", "object", "audio", "video",
             "handler", "listener", "link", "meta", "base"}
_SAFE_DATA_URI = re.compile(r"^data:image/(png|jpeg|gif|webp);base64,", re.I)
_CSS_URL = re.compile(r"url\(\s*['\"]?\s*(?!#)", re.I)


def looks_like_svg(data: bytes) -> bool:
    head = data[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:4096].lower())


def sanitize_svg(data: bytes) -> bytes | None:
    """A drawing-only copy of an SVG, or None if it isn't one we'll keep.

    <img> never runs an SVG's scripts, but the file is served from the UI's
    own origin, so opened directly it would. Belt and braces: this strips
    everything active, and logo_file() adds a CSP that forbids it anyway.
    DOCTYPEs are refused outright (entity expansion, external entities)."""
    import xml.etree.ElementTree as ET

    if len(data) > MAX_SVG_BYTES or not looks_like_svg(data):
        return None
    low = data.lower()
    if b"<!doctype" in low or b"<!entity" in low:
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None

    def local(tag) -> str:
        return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""

    if local(root.tag) != "svg":
        return None

    def clean(el) -> None:
        for child in list(el):
            name = local(child.tag)
            if not name or name in _SVG_DROP:  # comments / PIs have no str tag
                el.remove(child)
                continue
            if name in ("set", "animate", "animatetransform", "animatemotion") and \
                    "href" in (child.get("attributeName") or "").lower():
                el.remove(child)
                continue
            if name == "style" and ("@import" in (child.text or "").lower() or _CSS_URL.search(child.text or "")):
                el.remove(child)
                continue
            clean(child)
        for attr in list(el.attrib):
            a = local(attr)
            value = el.attrib[attr]
            if a.startswith("on"):
                del el.attrib[attr]
            elif a == "href" and not (value.startswith("#") or _SAFE_DATA_URI.match(value)):
                del el.attrib[attr]
            elif a == "style" and ("@import" in value.lower() or _CSS_URL.search(value)):
                del el.attrib[attr]
            elif _CSS_URL.search(value) and a in ("fill", "stroke", "filter", "mask", "clip-path",
                                                  "marker-start", "marker-mid", "marker-end"):
                del el.attrib[attr]

    clean(root)
    ET.register_namespace("", _SVG_NS)
    ET.register_namespace("xlink", _XLINK_NS)
    out = ET.tostring(root, encoding="utf-8", xml_declaration=False)
    return out if out else None


def logo_from_bytes(data: bytes, *, allow_svg: bool, min_side: int = 16) -> dict | None:
    """{"data", "ext", "side"} for an image we keep as a logo, else None. SVG
    only when the user supplied it (a scraped one is skipped); its "side"
    counts as large, since a vector scales without loss."""
    if allow_svg and looks_like_svg(data):
        clean = sanitize_svg(data)
        return {"data": clean, "ext": "svg", "side": 1024} if clean else None
    info = image_info(data)
    if not info:
        return None
    ext, w, h = info
    if w <= 0 or h <= 0 or max(w, h) > MAX_ASPECT * min(w, h) or min(w, h) < min_side:
        return None
    return {"data": data, "ext": ext, "side": min(w, h)}


def _download_image(url: str, allow_svg: bool = False) -> tuple[bytes, str, int] | None:
    """(bytes, ext, short side) for a usable logo image, else None."""

    def imageish(r: _Resp) -> bool:
        if r.status >= 400 or "html" in r.ctype:
            return False
        return allow_svg or "svg" not in r.ctype

    r = _open(url, public_only=True, max_bytes=MAX_IMAGE_BYTES, body_if=imageish)
    if r is None or not r.body or not imageish(r):
        return None
    if allow_svg:
        got = logo_from_bytes(r.body, allow_svg=True, min_side=1)
        return (got["data"], got["ext"], got["side"]) if got else None
    info = image_info(r.body)
    if not info:
        return None
    ext, w, h = info
    if w <= 0 or h <= 0 or max(w, h) > MAX_ASPECT * min(w, h):
        return None
    return r.body, ext, min(w, h)


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf"\b{name}\s*=\s*[\"']([^\"']*)[\"']", tag, re.I)
    return m.group(1) if m else None


def _declared_size(sizes: str | None) -> int:
    best = 0
    for part in (sizes or "").split():
        head = part.lower().split("x", 1)[0]
        if head.isdigit():
            best = max(best, int(head))
    return best


def icon_links(html: str, page_url: str) -> dict:
    touch, icons, manifest = [], [], None
    for m in LINK_TAG_RE.finditer(html):
        tag = m.group(0)
        rel = (_attr(tag, "rel") or "").lower()
        href = _attr(tag, "href")
        href = _resolve(page_url, href) if href else None
        if not rel or not href:
            continue
        size = _declared_size(_attr(tag, "sizes"))
        if "apple-touch-icon" in rel:
            touch.append((href, size))
        elif rel == "manifest":
            manifest = manifest or href
        elif "icon" in rel.split() and not _path_of(href).endswith(".svg"):
            icons.append((href, size))
    og = None
    for m in META_TAG_RE.finditer(html):
        tag = m.group(0)
        prop = (_attr(tag, "property") or _attr(tag, "name") or "").lower()
        if prop in ("og:image", "twitter:image"):
            content = _attr(tag, "content")
            og = _resolve(page_url, content) if content else None
            if og:
                break
    icons.sort(key=lambda t: -t[1])
    touch.sort(key=lambda t: -t[1])
    return {
        "touch": [u for u, _ in touch],
        "large": [u for u, s in icons if s >= GOOD_SIDE],
        "small": [u for u, s in icons if s < GOOD_SIDE],
        "og": og,
        "manifest": manifest,
    }


def manifest_icons(text: str, manifest_url: str) -> list[str]:
    try:
        icons = json.loads(text).get("icons") or []
    except (ValueError, AttributeError):
        return []
    out = []
    for o in icons:
        if not isinstance(o, dict) or not isinstance(o.get("src"), str):
            continue
        if _path_of(o["src"]).endswith(".svg"):
            continue
        u = _resolve(manifest_url, o["src"])
        if u:
            out.append((u, _declared_size(o.get("sizes") if isinstance(o.get("sizes"), str) else None)))
    return [u for u, _ in sorted(out, key=lambda t: -t[1])]


def _fetch_page(url: str, max_bytes: int = 400_000, *, public_only: bool = True) -> tuple[str | None, str]:
    """(HTML or None, the URL the redirects ended at — known even on a refusal)."""

    def textual(r: _Resp) -> bool:
        return "html" in r.ctype or "text" in r.ctype

    r = _open(url, public_only=public_only, max_bytes=max_bytes, body_if=textual)
    if r is None:
        return None, url
    if r.status >= 400 or not textual(r):
        return None, r.url
    return r.text(), r.url


def homepage_icons(homepage: str) -> list[str]:
    html, final = _fetch_page(homepage)
    p = urllib.parse.urlsplit(final)
    if not p.scheme or not p.netloc:
        return []
    origin = f"{p.scheme}://{p.netloc}"
    homepage = final
    links = icon_links(html, homepage) if html else {"touch": [], "large": [], "small": [], "og": None, "manifest": None}
    manifest = []
    if links["manifest"]:
        text = _fetch_text(links["manifest"], max_bytes=200_000, any_type=True)
        manifest = manifest_icons(text, links["manifest"]) if text else []
    out = [*links["touch"], *manifest, *links["large"]]
    if links["og"]:
        out.append(links["og"])
    out.append(f"{origin}/apple-touch-icon.png")
    out.extend(links["small"])
    out.append(f"{origin}/favicon.ico")
    return out


def _icy_info(stream_url: str) -> tuple[str | None, str | None]:
    """(icy-url, icy-name) the stream announces. The stream is the station's own
    URL, so it may be on the LAN; its icy-url is third-party data."""
    r = _open(stream_url, public_only=False, max_bytes=0, headers={"Icy-MetaData": "1"})
    if r is None or r.status >= 400:
        return None, None
    url = r.header("icy-url") or None
    if url and not url.lower().startswith("http"):
        url = f"https://{url}"
    if url and "." not in url:
        url = None
    name = r.header("icy-name")
    return url, (name if len(name) >= 3 else None)


def _name_queries(title: str, icy_name: str | None) -> list[str]:
    base = directory_query(title)
    words = [w for w in base.split(" ") if w]
    prefixes = [" ".join(words[:n]) for n in range(len(words) - 1, 1, -1)]
    out, keys = [], set()
    for q in [base, *( [directory_query(icy_name)] if icy_name else [] ), *prefixes]:
        k = _name_key(q)
        if len(k) >= 4 and k not in keys:
            keys.add(k)
            out.append(q)
    return out[:3]


def _pick_entry(found: list[dict], stream_url: str, names: list[str]) -> dict | None:
    key = _url_key(stream_url)
    for e in found:
        if _url_key(str(e.get("url") or "")) == key or _url_key(str(e.get("url_resolved") or "")) == key:
            return e
    sp = _site_path_key(stream_url)
    if sp:
        for e in found:
            if sp in (_site_path_key(str(e.get("url") or "")), _site_path_key(str(e.get("url_resolved") or ""))):
                return e
    keys = [k for k in (_name_key(n) for n in names) if k]
    named = [e for e in found if e.get("favicon")]
    for e in named:
        if _name_key(str(e.get("name") or "")) in keys:
            return e
    for e in named:
        k = _name_key(str(e.get("name") or ""))
        if len(k) >= 6 and any(n.startswith(k) for n in keys):
            return e
    return None


def _directory_entry(stream_url: str, title: str, icy_name: str | None) -> dict | None:
    data = _rb_get(f"/json/stations/byurl?url={urllib.parse.quote(stream_url, safe='')}")
    if data and isinstance(data[0], dict):
        return data[0]
    for q in _name_queries(title, icy_name):
        found = _rb_get(
            f"/json/stations/search?name={urllib.parse.quote(q)}&limit=30&hidebroken=true&order=votes&reverse=true"
        )
        entry = _pick_entry([e for e in (found or []) if isinstance(e, dict)], stream_url, [title, *( [icy_name] if icy_name else [] )])
        if entry:
            return entry
    return None


def find_logo(*args, **kwargs) -> dict | None:
    try:
        return _find_logo(*args, **kwargs)
    finally:
        release_memory()


def _find_logo(
    stream_url: str, name: str, homepage: str | None = None, hint: str | None = None, only_hint: bool = False
) -> dict | None:
    """{"data": bytes, "ext", "side", "source": image URL} for the best logo, or None.
    [only_hint]: the user pasted this image themselves — use it or nothing."""
    if hint and hint.lower().startswith("http"):
        got = _download_image(hint, allow_svg=only_hint)
        if got and got[2] >= (16 if only_hint else MIN_SIDE):
            return {"data": got[0], "ext": got[1], "side": got[2], "source": hint}
    if only_hint:
        return None
    icy_url, icy_name = _icy_info(stream_url) if stream_url else (None, None)
    entry = _directory_entry(stream_url, name, icy_name) if stream_url else None
    home = homepage if homepage and homepage.lower().startswith("http") else None
    if not home and entry and str(entry.get("homepage") or "").lower().startswith("http"):
        home = entry["homepage"]
    home = home or icy_url
    candidates: list[str] = []
    fav = str((entry or {}).get("favicon") or "")
    if fav.lower().startswith("http"):
        candidates.append(fav)
    if home:
        candidates.extend(homepage_icons(home))
    seen: set[str] = set()
    ordered = [u for u in candidates if not (u in seen or seen.add(u))][:MAX_IMAGE_TRIES]
    fallback = None
    for u in ordered:
        got = _download_image(u)
        if not got:
            continue
        data, ext, side = got
        if side >= GOOD_SIDE:
            return {"data": data, "ext": ext, "side": side, "source": u}
        if side >= MIN_SIDE and side > (fallback["side"] if fallback else 0):
            fallback = {"data": data, "ext": ext, "side": side, "source": u}
    return fallback


def store_logo(found: dict) -> str:
    """Writes the image content-addressed under LOGO_DIR; returns its file name."""
    name = f"{hashlib.sha1(found['data']).hexdigest()[:16]}.{found['ext']}"
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    path = LOGO_DIR / name
    if not path.exists():
        tmp = path.with_suffix(".part")
        # Synced before the rename: the name is the content's hash and an
        # existing file is never rewritten, so a power cut that left it empty
        # would serve a blank logo for good.
        with open(tmp, "wb") as fh:
            fh.write(found["data"])
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    return name


def prune_logos(referenced: set[str]) -> int:
    """Removes logo files no station points at any more."""
    removed = 0
    try:
        entries = list(LOGO_DIR.iterdir())
    except OSError:
        return 0
    for p in entries:
        if LOGO_NAME_RE.match(p.name) and p.name not in referenced:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# ---- Batch -------------------------------------------------------------------------


class LogoBatch:
    """One background pass over the station list (Settings → Sources)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = {"running": False, "total": 0, "done": 0, "found": 0, "missing": [], "current": None}

    def status(self) -> dict:
        with self._lock:
            return {**self._state, "missing": list(self._state["missing"])}

    def start(self, player, only_missing: bool = True) -> dict:
        with self._lock:
            if self._state["running"]:
                return {**self._state, "missing": list(self._state["missing"])}
            streams = player.list_streams(include_disabled=True)
            todo = [s for s in streams if not (only_missing and s.get("logo"))]
            self._state = {"running": True, "total": len(todo), "done": 0, "found": 0, "missing": [], "current": None}
        threading.Thread(target=self._run, args=(player, todo), name="logo-batch", daemon=True).start()
        return self.status()

    def _one(self, player, s: dict) -> None:
        with self._lock:
            self._state["current"] = s.get("name")
        ok = False
        try:
            found = _find_logo(s.get("url", ""), s.get("name", ""), s.get("homepage"))
            if found:
                ok = player.set_stream_logo(s["id"], found) is not None
        except Exception:  # noqa: BLE001 - one bad site must not stop the pass
            ok = False
        with self._lock:
            self._state["done"] += 1
            if ok:
                self._state["found"] += 1
            else:
                self._state["missing"].append(s.get("name") or s.get("id"))

    def _run(self, player, todo: list[dict]) -> None:
        try:
            # Two stations at a time: each fans out into its own fetches, and
            # this is a 512 MB board that is usually playing at the same time.
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda s: self._one(player, s), todo))
        finally:
            release_memory()
            with self._lock:
                self._state["running"] = False
                self._state["current"] = None


logo_batch = LogoBatch()


# ---- Health check --------------------------------------------------------------------


def diagnose(url: str) -> str:
    """A short human reason why [url] does not play."""
    try:
        host = urllib.parse.urlsplit(url).hostname
        if not host:
            return "Not a valid URL"
        socket.getaddrinfo(host, None)
    except (OSError, ValueError):
        return "Server not found"
    if not host_allowed(url, False):
        return "Address not allowed (this board or link-local)"
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Icy-MetaData": "1"})
    try:
        with urllib.request.build_opener(_Redirects(False)).open(req, timeout=TIMEOUT) as r:
            ct = (r.headers.get("Content-Type") or "").split(";", 1)[0].strip() or "unknown type"
            return f"Not an audio stream ({ct})"
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}"
    except http.client.BadStatusLine:
        return "Unexpected answer from the server"
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, socket.timeout) or "timed out" in str(reason):
            return "No response (timeout)"
        if "CERTIFICATE" in str(reason).upper() or "SSL" in str(reason).upper():
            return "Security certificate problem"
        if isinstance(reason, ConnectionRefusedError):
            return "Connection refused"
        return "Can't connect"
    except (TimeoutError, socket.timeout):
        return "No response (timeout)"
    except (OSError, http.client.HTTPException):
        return "Can't connect"


def check_stream(url: str) -> str | None:
    """None when [url] answers like a stream; otherwise a reason. A failure is
    retried once after a pause, so a brief hiccup doesn't flag a working station."""
    if sniff(url, public_only=False):
        return None
    time.sleep(1.5)
    if sniff(url, public_only=False):
        return None
    return diagnose(url)


def alternatives(stream: dict) -> list[dict]:
    """Replacement streams for a dead station, verified now: its website first
    (often the mount just moved), then the Radio Browser directory by name."""
    out: list[dict] = []
    try:
        home = (stream.get("homepage") or "").strip()
        if home:
            out.extend({**c, "source": "site"} for c in (_probe(home, public_only=True).get("streams") or []))
        q = directory_query(stream.get("name") or "")
        if len(_name_key(q)) >= 3:
            data = _rb_get(
                f"/json/stations/search?name={urllib.parse.quote(q)}&limit=12&hidebroken=true&order=votes&reverse=true"
            )
            found = [_rb_candidate(e) for e in (data or []) if isinstance(e, dict)]
            found = [c for c in found if c["url"].lower().startswith(("http://", "https://"))]
            live = _pmap(lambda c: c if sniff(c["url"], public_only=True) else None, found)
            out.extend(c for c in live if c)
        # Website results first (they are this station by definition), then the
        # directory by how closely the name matches, bitrate breaking ties —
        # "RADIO ZEN" must outrank "RADIO ZENIT" whatever their bitrates.
        own = _name_key(stream.get("name") or "")

        def closeness(c: dict) -> int:
            k = _name_key(c.get("name") or "")
            if not k or not own:
                return 3
            if k == own:
                return 0
            if k.startswith(own) or own.startswith(k):
                return 1 if abs(len(k) - len(own)) <= 3 else 2
            return 2 if own in k or k in own else 3

        site = best_first([c for c in out if c.get("source") == "site"])
        directory = sorted(
            best_first([c for c in out if c.get("source") != "site"]), key=closeness
        )
        current = _url_key(stream.get("url") or "")
        seen, uniq = set(), []
        for c in site + directory:
            k = _url_key(c["url"])
            if k != current and k not in seen:
                seen.add(k)
                uniq.append(c)
        return uniq[:10]
    finally:
        release_memory()


class HealthCheck:
    """Settings → Sources: is every station still on the air?"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict = {"running": False, "total": 0, "done": 0, "results": [], "checked_at": None}

    def status(self) -> dict:
        with self._lock:
            return {**self._state, "results": [dict(r) for r in self._state["results"]]}

    def start(self, player) -> dict:
        with self._lock:
            if self._state["running"]:
                return {**self._state, "results": list(self._state["results"])}
            todo = player.list_streams(include_disabled=True)
            self._state = {"running": True, "total": len(todo), "done": 0, "results": [], "checked_at": None}
        threading.Thread(target=self._run, args=(todo,), name="stream-check", daemon=True).start()
        return self.status()

    def forget(self, stream_id: str) -> None:
        """Drops a result once its problem was dealt with (deleted, fixed)."""
        with self._lock:
            self._state["results"] = [r for r in self._state["results"] if r["id"] != stream_id]

    def _one(self, s: dict) -> None:
        try:
            reason = check_stream(s.get("url", ""))
        except Exception as exc:  # noqa: BLE001
            reason = f"Check failed: {exc}"
        with self._lock:
            self._state["done"] += 1
            self._state["results"].append(
                {
                    "id": s.get("id"),
                    "name": s.get("name"),
                    "url": s.get("url"),
                    "ok": reason is None,
                    "reason": reason,
                    "disabled": bool(s.get("disabled")),
                }
            )

    def _run(self, todo: list[dict]) -> None:
        try:
            with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
                list(pool.map(self._one, todo))
        finally:
            release_memory()
            with self._lock:
                self._state["running"] = False
                self._state["checked_at"] = time.time()


health_check = HealthCheck()

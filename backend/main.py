"""FastAPI backend — Phase 3."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
import urllib.parse
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from backend.textsafe import SafeJSONResponse
from backend.config import INSTALL_DIR, cfg_get, cfg_set, get_links, get_player, get_radio
from backend.services.links import PLATFORM_LABELS, LinkError, ytdl_path
from backend.services import station_lookup, stream_settings
from backend.services.radio import RadioService
from backend.services.mpv import MpvError
from backend.services.xq_probe import XqProbe
from backend.services.bluetooth import (
    list_devices,
    pair_device,
    remove_device,
    scan_devices,
    select_codec,
    set_primary,
    start_scan,
    watchdog_info,
)
from backend.services import files as files_svc
from backend.services import sources as sources_svc
from backend.services.leds import BlinktLeds, driver_usable, radio_overlay_configured
from backend.services.network import (
    add_wifi,
    captive_start,
    captive_stop,
    forget_wifi,
    read_network_status,
    scan_wifi,
)
from backend.services.system import (
    bt_connect,
    full_status,
    read_bt_status,
    refresh_bt_status,
    set_bt_reconnect_hook,
)

_ws_clients: set[WebSocket] = set()
_status_task: asyncio.Task | None = None
_viz_task: asyncio.Task | None = None
_viz_clients_active = False

# Blinkt LED VU meter (physical mirror of the on-screen meter).
_leds: BlinktLeds | None = None
_leds_enabled = False
_leds_brightness = 0.5
_led_stop = threading.Event()


def _led_worker() -> None:
    # Read the (delayed) VU levels and drive the LEDs at ~30 Hz while enabled.
    # No mpv IPC here — the analyzer tap is kept alive/ticked by _viz_loop.
    # The Blinkt driver is created here: lgpio requires the handle to be opened
    # and used on the same thread. It exists only while the LEDs are on, because
    # claiming the pins and blanking the strip clocks GPIO 23, which is the
    # tuner's reset line on a board fitted with a DABBoard. A driver that failed
    # stays put until the switch goes off, so it isn't retried four times a second.
    global _leds
    while not _led_stop.is_set():
        if _leds_enabled:
            if _leds is None:
                _leds = BlinktLeds()
            if _leds.available:
                try:
                    _leds.render(get_player().spectrum.levels(), _leds_brightness)
                except Exception:
                    pass
                time.sleep(0.033)
                continue
        elif _leds is not None:
            _leds.close()
            _leds = None
        time.sleep(0.25)
    if _leds is not None:
        _leds.close()
        _leds = None


def _leds_fitted() -> bool:
    # An APA102 strip can't be detected, so the owner says. Unset, the board decides:
    # on a 3 A+ GPIO 23 may be the tuner's reset, and one stray tap on the LED switch
    # would clock it.
    value = str(cfg_get("leds.fitted")).lower()
    if value in ("true", "1", "yes"):
        return True
    if value in ("false", "0", "no"):
        return False
    return not RadioService.board_supported()


def _radio_active() -> bool:
    # Either signal is enough: the setting is what was asked for, the overlay is
    # what the board will actually boot with.
    wanted = str(cfg_get("radio.enabled") or "false").lower() in ("true", "1", "yes")
    return wanted or radio_overlay_configured()


def _dump(model: BaseModel) -> dict:
    # Pydantic v1 (Debian Bookworm ships 1.10) uses .dict(); v2 uses .model_dump().
    return model.model_dump() if hasattr(model, "model_dump") else model.dict()


def require_token(x_safydo_token: str | None = Header(default=None, alias="X-Safydo-Token")) -> None:
    expected = os.environ.get("PLAYER_API_TOKEN", "")
    if expected and x_safydo_token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


async def _viz_loop() -> None:
    # Manage the analyzer tap as WS clients come and go: attach on the first
    # client, detach on the last, and periodically re-attach if it was lost.
    # The 20 Hz level ticking lives in _broadcast_status (the loop that actually
    # sends the frames) so levels stay fresh even if an mpv IPC call here stalls.
    global _viz_clients_active
    ensure_ticks = 0
    while True:
        # LEDs need the tap too, even with no web client watching.
        leds_only = _leds_enabled and not _ws_clients
        active = bool(_ws_clients) or _leds_enabled
        player = get_player()
        # Guarded: an mpv IPC timeout (busy load, FIFO stall) raises OSError,
        # and an escaping one ended this task for good — no tap re-attached,
        # no LED ticks — until the backend restarted.
        try:
            if active != _viz_clients_active:
                _viz_clients_active = active
                ensure_ticks = 0
                await asyncio.to_thread(player.set_viz_active, active)
            elif active:
                ensure_ticks += 1
                if ensure_ticks >= 10:  # ~ every 5 s (0.5 s poll) — cheap re-check
                    ensure_ticks = 0
                    await asyncio.to_thread(player.ensure_viz_filters)
        except Exception as exc:  # noqa: BLE001
            print(f"viz loop: {type(exc).__name__}: {exc}", flush=True)
        if leds_only:
            # No _broadcast_status running (no web client) — tick the analyzer
            # here at 20 Hz so the LED meter still gets fresh levels.
            await asyncio.to_thread(player.spectrum.tick, analyze=True)
            await asyncio.sleep(0.05)
        else:
            await asyncio.sleep(0.5)


async def _ensure_viz_for_clients() -> None:
    if not _ws_clients:
        return
    await asyncio.to_thread(get_player().set_viz_active, True)


async def _broadcast_status() -> None:
    # Two cadences on one socket: tiny spectrum-only frames at 20 Hz keep the VU
    # meter fluid; the heavyweight full status goes out once a second. This loop
    # also *ticks* the analyzer each iteration (a cheap tmpfs read) so the level
    # it sends is always freshly computed — no reliance on a separate ticker.
    FULL_EVERY = 20
    frame = 0
    while _ws_clients:
        player = get_player()
        bands = await asyncio.to_thread(player.spectrum.tick, analyze=True)
        if frame % FULL_EVERY == 0:

            def _status_with_spectrum() -> dict:
                body = full_status(player, deep_check=False)
                # bands is [left, right] channel levels (stereo VU meter).
                body["spectrum"] = {"bands": bands, "active": True}
                return body

            try:
                body = await asyncio.to_thread(_status_with_spectrum)
            except Exception as exc:  # noqa: BLE001 — one bad read mustn't end the stream
                print(f"status broadcast: {type(exc).__name__}: {exc}", flush=True)
                body = {"type": "spectrum", "bands": bands}
        else:
            body = {"type": "spectrum", "bands": bands}
        frame += 1
        if not _ws_clients:
            break
        payload = json.dumps(body)
        dead: list[WebSocket] = []
        for ws in list(_ws_clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            _ws_clients.discard(ws)
        await asyncio.sleep(0.05)


def _flag(key: str, default: bool) -> bool:
    # Not `cfg_get(key) or default`: a saved false is falsy, and that spelling
    # silently turned audio.resume_on_boot: false back into true.
    value = cfg_get(key)
    if value is None or value == "":
        return default
    return str(value).lower() in ("true", "1", "yes")


def _mpv_idle() -> bool | None:
    """None: no mpv to ask. Blocking IPC (up to the client's 8 s timeout), so
    callers run it in a thread — on the event loop a busy mpv froze every
    request and the status stream with it."""
    mpv = get_player().mpv
    if not mpv.available():
        return None
    return bool(mpv.get_property_safe("idle-active", True))


async def _resume_on_boot() -> None:
    player = get_player()
    if player.saved_state().get("source") == "radio":
        get_radio().resume_after_boot()
        return
    if not _flag("audio.resume_on_boot", True):
        return
    for _ in range(45):
        await asyncio.sleep(2)
        try:
            idle = await asyncio.to_thread(_mpv_idle)
            if idle is None:
                continue
            if idle:
                await asyncio.to_thread(player.resume_last)
            return
        except (MpvError, OSError, ValueError, json.JSONDecodeError):
            continue


async def _resume_if_idle(wait_mpv: float = 0.0) -> bool:
    """Restart the last stream/track after a BT reconnect or codec switch
    interrupted mpv (an ALSA write error drops mpv back to idle).

    wait_mpv: seconds to wait for the mpv socket, for flows that just
    restarted audio-player-mpv.service (e.g. set_primary)."""
    try:
        waited = 0.0
        while (idle := await asyncio.to_thread(_mpv_idle)) is None:
            if waited >= wait_mpv:
                return False
            await asyncio.sleep(1)
            waited += 1
        if not idle:
            return False
        result = await asyncio.to_thread(get_player().resume_last)
        return result is not None
    except (MpvError, OSError, ValueError, json.JSONDecodeError):
        return False


def _same_origin(scope: dict) -> bool:
    """True unless a browser sent this on behalf of another site.

    Sec-Fetch-Site is the browser's own verdict; older browsers only send
    Origin, compared with the Host nginx passes on. No header at all is not a
    browser (curl from the shell scripts, the TUI), which CSRF cannot reach.
    """
    headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers") or []}
    site = headers.get("sec-fetch-site")
    if site:
        return site in ("same-origin", "none")
    origin = headers.get("origin")
    if not origin:
        return True
    try:
        origin_host = urllib.parse.urlsplit(origin).hostname
        host = urllib.parse.urlsplit("//" + headers.get("host", "")).hostname
    except ValueError:
        return False
    return bool(origin_host) and origin_host == host


class _SameOriginOnly:
    """Refuses cross-site writes and WebSockets.

    Dropping CORS only stops another site *reading* the answers. A POST without
    a body (shutdown, reboot, captive/start, recordings/start, transport/*) is a
    "simple" request — no preflight — so any page a LAN user opened could still
    fire it with mode: "no-cors", and open /ws/status to read the full status.
    """

    SAFE = frozenset({"GET", "HEAD", "OPTIONS"})

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        kind = scope["type"]
        guarded = kind == "websocket" or (kind == "http" and scope["method"] not in self.SAFE)
        if guarded and not _same_origin(scope):
            if kind == "websocket":
                await receive()  # websocket.connect
                await send({"type": "websocket.close", "code": 1008})
                return
            body = b'{"detail":"cross-site request refused"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


# SafeJSONResponse: a file name holding raw bytes must not turn a listing into a 500.
app = FastAPI(title="RPI Audio Player", version="0.3.0", default_response_class=SafeJSONResponse)
# No CORS middleware on purpose: the UI is served same-origin by nginx (and the
# Vite dev server proxies /api), so nothing legitimate is cross-origin. With
# allow_origins=["*"] any page a LAN user opened could drive this root backend
# and read the answers, e.g. make /api/streams/probe fetch LAN pages for it.
# Not reading is not enough: _SameOriginOnly stops it sending, too.
app.add_middleware(_SameOriginOnly)

_resume_task: asyncio.Task | None = None


# NOTE: FastAPI 0.92 (Debian Bookworm) predates the `lifespan=` parameter — it
# silently ignores it — so use the on_event handlers, which this version does
# support. (Passing lifespan= here ran the default no-op lifespan, so none of
# this startup code executed.)
@app.on_event("startup")
async def _on_startup() -> None:
    global _status_task, _viz_task, _resume_task, _leds_enabled, _leds_brightness
    try:
        await asyncio.to_thread(get_player().recorder.startup)
    except Exception:  # noqa: BLE001 — never block startup over leftovers
        pass
    # Device-initiated reconnects (speaker powered back on) resume playback.
    loop = asyncio.get_running_loop()

    def _on_bt_reconnected() -> None:
        asyncio.run_coroutine_threadsafe(_resume_if_idle(wait_mpv=8), loop)

    set_bt_reconnect_hook(_on_bt_reconnected)

    # Remount configured network drives (SMB/NFS) after a reboot — in the
    # background: awaited here, every unreachable NAS held the whole server
    # (UI and API) behind a mount timeout of up to 30 s.
    def _remount() -> None:
        try:
            files_svc.remount_all()
        except Exception:  # noqa: BLE001 — a flaky NAS must not matter
            pass

    threading.Thread(target=_remount, daemon=True, name="nas-remount").start()
    # Blinkt LED VU meter — load saved state; the worker thread creates the
    # driver itself (lgpio handle must be opened on the thread that uses it).
    # The tuner wins over a saved "on": the tap and the worker stay idle, and the
    # saved value is left alone.
    _leds_enabled = (
        str(cfg_get("leds.enabled") or "false").lower() in ("true", "1", "yes")
        and not _radio_active()
        and _leds_fitted()
    )
    try:
        _leds_brightness = max(0.01, min(1.0, float(cfg_get("leds.brightness") or 0.5)))
    except (TypeError, ValueError):
        _leds_brightness = 0.5
    threading.Thread(target=_led_worker, daemon=True, name="blinkt-vu").start()
    get_radio().start_worker()
    XqProbe(get_player().mpv, on_reconnected=_on_bt_reconnected).start()
    _resume_task = asyncio.create_task(_resume_on_boot())
    _viz_task = asyncio.create_task(_viz_loop())


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    if _resume_task is not None:
        _resume_task.cancel()
    if _viz_task and not _viz_task.done():
        _viz_task.cancel()
    if _status_task and not _status_task.done():
        _status_task.cancel()
    _led_stop.set()  # blank the LEDs (worker owns the lgpio handle)
    get_radio().stop_worker()  # leaves the chip and capture running on purpose


class PlayUrlBody(BaseModel):
    url: str
    name: str | None = None
    genre: str | None = None
    # False for a finite file (a Jamendo track): full seek bar, no recording.
    # None keeps the old default — an arbitrary URL is treated as radio.
    live: bool | None = None


class LinkBody(BaseModel):
    url: str


class PlaylistCreateBody(BaseModel):
    name: str = ""


class PlaylistUpdateBody(BaseModel):
    name: str | None = None
    link_ids: list[str] | None = None
    shuffle: bool | None = None
    loop: bool | None = None


class StreamSettingsBody(BaseModel):
    retries: int | None = None
    reconnect_delay_max: int | None = None
    cache_secs: int | None = None
    rebuffer_secs: float | None = None
    prebuffer_on_start: bool | None = None
    timeout_secs: int | None = None
    user_agent: str | None = None
    output_buffer: float | None = None
    reset: bool = False


class CookiesBody(BaseModel):
    content: str = ""


class SeekBody(BaseModel):
    position: float = 0.0


class PlayFileBody(BaseModel):
    path: str


class VolumeBody(BaseModel):
    level: int = Field(ge=0, le=100)


class VolumeFollowBody(BaseModel):
    enabled: bool


class EqBody(BaseModel):
    enabled: bool


class EqBandsBody(BaseModel):
    bands: list[float]


class NetworkDriveBody(BaseModel):
    type: str
    host: str
    share: str
    label: str = ""
    username: str = ""
    password: str = ""
    domain: str = ""
    options: str = ""


class StreamBody(BaseModel):
    name: str
    url: str
    genre: str = ""
    category: str = "Radio"
    favorite: bool = False
    homepage: str = ""


class ProbeBody(BaseModel):
    input: str


class FavoriteBody(BaseModel):
    favorite: bool


class LogoFindBody(BaseModel):
    # Overrides for a row edited but not saved yet; the saved station otherwise.
    url: str | None = None
    name: str | None = None
    homepage: str | None = None
    hint: str | None = None
    # The user pasted [hint] as the logo: use that image or report failure.
    only_hint: bool = False


class LogoBatchBody(BaseModel):
    only_missing: bool = True


class StreamPatchBody(BaseModel):
    name: str | None = None
    url: str | None = None
    homepage: str | None = None
    disabled: bool | None = None
    favorite: bool | None = None


class StreamsBody(BaseModel):
    streams: list[dict]


# Panels that can be switched off in Settings -> General. Neither keeps a
# process running while idle, so "off" mostly declutters the UI; Web links
# also stops its weekly yt-dlp upgrade timer.
MODULES = ("links", "files", "recordings")


class ConfigBody(BaseModel):
    resume_on_boot: bool | None = None
    autoconnect_strategy: str | None = None
    wedge_reboot: bool | None = None
    leds_enabled: bool | None = None
    leds_brightness: float | None = None
    leds_fitted: bool | None = None
    radio_enabled: bool | None = None
    radio_resume_on_boot: bool | None = None
    radio_rds_region: str | None = None
    source_enabled: dict[str, bool] | None = None
    jamendo_client_id: str | None = None
    rewind_minutes: int | None = None
    modules: dict[str, bool] | None = None
    recording: dict[str, Any] | None = None


class RadioPlayBody(BaseModel):
    frequency_khz: int | None = None


class RadioTuneBody(BaseModel):
    frequency_khz: int


class RadioSeekBody(BaseModel):
    up: bool = True


class RadioMonoBody(BaseModel):
    enabled: bool


class RadioPresetBody(BaseModel):
    name: str = ""
    frequency_khz: int


class BtConnectBody(BaseModel):
    mac: str | None = None


class BtMacBody(BaseModel):
    mac: str


class BtCodecBody(BaseModel):
    codec: str
    mac: str | None = None


class WifiBody(BaseModel):
    ssid: str
    password: str = ""


def _handle_player_error(exc: Exception) -> None:
    if isinstance(exc, MpvError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, OSError):
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    raise exc


async def _run_player(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as exc:
        _handle_player_error(exc)


@app.get("/health")
@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "phase": 3}


@app.get("/api/status")
async def status() -> dict[str, Any]:
    return await asyncio.to_thread(full_status, get_player(), deep_check=True)


@app.get("/api/streams")
async def list_streams(
    favorites: bool = Query(default=False), all: bool = Query(default=False)
) -> list[dict]:
    # all=True includes disabled stations (for the editor/backup); the browser
    # uses the default so disabled stations stay hidden from the play list.
    return get_player().list_streams(favorites_only=favorites, include_disabled=all)


@app.get("/api/bt/signal")
async def bt_signal() -> dict:
    """Live link quality/RSSI, read only on request: the Bluetooth panel polls
    this while it is open. Cached 5 s in link_signal(), so several open tabs
    still cost one HCI read pair per 5 s, and none when nobody is looking."""
    from backend.services.bluetooth import link_signal
    from backend.services.system import read_bt_status

    status = await asyncio.to_thread(read_bt_status)
    if not status.get("connected") or not status.get("mac"):
        return {"signal": None}
    return {"signal": await asyncio.to_thread(link_signal, status["mac"])}


@app.post("/api/streams/probe", dependencies=[Depends(require_token)])
async def probe_stream(body: ProbeBody) -> dict:
    """A website (afc.fm) or a stream URL → the playable streams found there."""
    return await asyncio.to_thread(station_lookup.probe, body.input)


@app.get("/api/streams/logos/batch")
async def logo_batch_status() -> dict:
    return station_lookup.logo_batch.status()


@app.post("/api/streams/logos/batch", dependencies=[Depends(require_token)])
async def logo_batch_start(body: LogoBatchBody) -> dict:
    return await asyncio.to_thread(station_lookup.logo_batch.start, get_player(), body.only_missing)


@app.get("/api/streams/check")
async def stream_check_status() -> dict:
    return station_lookup.health_check.status()


@app.post("/api/streams/check", dependencies=[Depends(require_token)])
async def stream_check_start() -> dict:
    return await asyncio.to_thread(station_lookup.health_check.start, get_player())


@app.get("/api/streams/{stream_id}/alternatives", dependencies=[Depends(require_token)])
async def stream_alternatives(stream_id: str) -> list[dict]:
    stream = get_player().get_stream(stream_id)
    if not stream:
        raise HTTPException(status_code=404, detail=f"unknown stream: {stream_id}")
    return await asyncio.to_thread(station_lookup.alternatives, stream)


@app.patch("/api/streams/{stream_id}", dependencies=[Depends(require_token)])
async def patch_stream(stream_id: str, body: StreamPatchBody) -> dict:
    fields = {k: v for k, v in _dump(body).items() if v is not None}
    try:
        stream = await asyncio.to_thread(get_player().update_stream, stream_id, fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not stream:
        raise HTTPException(status_code=404, detail=f"unknown stream: {stream_id}")
    if "url" in fields:
        station_lookup.health_check.forget(stream_id)
    return stream


@app.get("/api/logos/{name}")
async def logo_file(name: str) -> FileResponse:
    if not station_lookup.LOGO_NAME_RE.match(name):
        raise HTTPException(status_code=404, detail="no such logo")
    path = station_lookup.LOGO_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such logo")
    ext = name.rsplit(".", 1)[1]
    media = {"jpg": "image/jpeg", "ico": "image/x-icon", "svg": "image/svg+xml"}.get(ext, f"image/{ext}")
    # Content-addressed names: a new logo is a new URL, so cache for good.
    headers = {
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
    }
    if ext == "svg":
        # Sanitised on the way in; this keeps it inert even if opened directly.
        headers["Content-Security-Policy"] = station_lookup.SVG_CSP
    return FileResponse(path, media_type=media, headers=headers)


@app.post("/api/streams/{stream_id}/logo/upload", dependencies=[Depends(require_token)])
async def upload_stream_logo(stream_id: str, request: Request) -> dict:
    """The request body is the image itself: PNG, JPEG, GIF, WebP, ICO or SVG
    (an uploaded or pasted file, or pasted SVG markup)."""
    player = get_player()
    if not player.get_stream(stream_id):
        raise HTTPException(status_code=404, detail=f"unknown stream: {stream_id}")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > station_lookup.MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="image too large (3 MB at most)")
    data = b""
    async for chunk in request.stream():
        data += chunk
        if len(data) > station_lookup.MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="image too large (3 MB at most)")
    found = await asyncio.to_thread(station_lookup.logo_from_bytes, data, allow_svg=True)
    if not found:
        raise HTTPException(
            status_code=400,
            detail="Not an image we can use: PNG, JPEG, GIF, WebP, ICO or SVG (no scripts, no external links)",
        )
    saved = await asyncio.to_thread(player.set_stream_logo, stream_id, found)
    return {"stream": saved, "found": True, "source": "upload"}


@app.post("/api/streams/{stream_id}/favorite", dependencies=[Depends(require_token)])
async def set_favorite(stream_id: str, body: FavoriteBody) -> dict:
    stream = await asyncio.to_thread(get_player().update_stream, stream_id, {"favorite": body.favorite})
    if not stream:
        raise HTTPException(status_code=404, detail=f"unknown stream: {stream_id}")
    return stream


@app.post("/api/streams/{stream_id}/logo", dependencies=[Depends(require_token)])
async def find_stream_logo(stream_id: str, body: LogoFindBody) -> dict:
    """Searches the web for this station's logo and stores the best one found."""
    player = get_player()
    stream = player.get_stream(stream_id)
    if not stream:
        raise HTTPException(status_code=404, detail=f"unknown stream: {stream_id}")
    found = await asyncio.to_thread(
        station_lookup.find_logo,
        (body.url or stream.get("url") or "").strip(),
        (body.name or stream.get("name") or "").strip(),
        (body.homepage or stream.get("homepage") or "").strip() or None,
        (body.hint or "").strip() or None,
        body.only_hint,
    )
    if not found:
        return {"stream": stream, "found": False}
    saved = await asyncio.to_thread(player.set_stream_logo, stream_id, found)
    return {"stream": saved or stream, "found": True, "source": found["source"]}


@app.delete("/api/streams/{stream_id}/logo", dependencies=[Depends(require_token)])
async def clear_stream_logo(stream_id: str) -> dict:
    saved = await asyncio.to_thread(get_player().set_stream_logo, stream_id, None)
    if not saved:
        raise HTTPException(status_code=404, detail=f"unknown stream: {stream_id}")
    return saved


@app.get("/api/streams/{stream_id}")
async def get_stream(stream_id: str) -> dict:
    stream = get_player().get_stream(stream_id)
    if not stream:
        raise HTTPException(status_code=404, detail=f"unknown stream: {stream_id}")
    return stream


@app.post("/api/streams", dependencies=[Depends(require_token)])
async def add_stream(body: StreamBody) -> dict:
    try:
        return await asyncio.to_thread(get_player().add_stream, _dump(body))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/streams", dependencies=[Depends(require_token)])
async def replace_streams(body: StreamsBody) -> list[dict]:
    try:
        return await asyncio.to_thread(get_player().replace_streams, body.streams)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/streams/{stream_id}", dependencies=[Depends(require_token)])
async def delete_stream(stream_id: str) -> dict:
    ok = await asyncio.to_thread(get_player().delete_stream, stream_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"unknown stream: {stream_id}")
    station_lookup.health_check.forget(stream_id)
    return {"ok": True, "removed": stream_id}


@app.get("/api/sources")
async def sources_list() -> list[dict]:
    return await asyncio.to_thread(sources_svc.list_sources)


@app.get("/api/sources/{source_id}/genres")
async def sources_genres(source_id: str) -> list[str]:
    return await asyncio.to_thread(sources_svc.genres, source_id)


@app.get("/api/sources/{source_id}/browse")
async def sources_browse(
    source_id: str,
    genre: str | None = Query(default=None),
    q: str | None = Query(default=None),
    page: int = Query(default=0, ge=0),
) -> list[dict]:
    return await asyncio.to_thread(sources_svc.browse, source_id, genre, q, page)


@app.get("/api/config")
async def get_config() -> dict:
    def _read() -> dict:
        from datetime import datetime

        streams_path = INSTALL_DIR / "config" / "streams.json"
        version, count = None, 0
        try:
            version = datetime.fromtimestamp(streams_path.stat().st_mtime).strftime("%Y%m%d-%H%M%S")
            count = len(json.loads(streams_path.read_text()).get("streams") or [])
        except (OSError, json.JSONDecodeError):
            pass
        leds = _leds  # the worker can drop it between two reads
        return {
            "hostname": cfg_get("hostname") or "safydo",
            "mdns": cfg_get("mdns_name") or "safydo.local",
            "resume_on_boot": _flag("audio.resume_on_boot", True),
            "autoconnect_strategy": str(cfg_get("bluetooth.autoconnect_strategy") or "strongest"),
            "wedge_reboot": str(cfg_get("bluetooth.wedge_reboot") or "false").lower()
            in ("true", "1", "yes"),
            # yes|no|unknown — anything but "yes" means a wedged controller can
            # only be recovered by a reboot, which is what the switch above buys.
            "wedge_rebind_viable": watchdog_info().get("rebind_viable", "unknown"),
            "streams_version": version,
            "streams_count": count,
            "leds_enabled": _leds_enabled,
            "leds_brightness": _leds_brightness,
            # A strip can't be detected (APA102 is write-only), so with the LEDs
            # off this only says the driver can be tried. A failure shows up once
            # they're on.
            "leds_available": driver_usable(leds),
            "leds_error": leds.error if leds is not None else "",
            "leds_fitted": _leds_fitted(),
            "radio_enabled": _radio_active(),
            "radio_state": get_radio().state(),
            "radio_resume_on_boot": _flag("radio.resume_on_boot", False),
            "radio_rds_region": get_radio()._region(),
            "sources": sources_svc.list_sources(),
            "rewind_minutes": get_player().rewind_minutes(),
            "modules": {m: _flag(f"modules.{m}", True) for m in MODULES},
            "recording": get_player().recorder.settings(),
            **get_player().rewind_info(),
        }

    return await asyncio.to_thread(_read)


@app.put("/api/config", dependencies=[Depends(require_token)])
async def put_config(body: ConfigBody) -> dict:
    global _leds_enabled, _leds_brightness

    # Checked before anything is written, and only when this request turns one of
    # them on: an unrelated setting mustn't start failing because of this rule.
    fitted_next = await asyncio.to_thread(_leds_fitted) if body.leds_fitted is None else body.leds_fitted
    if body.leds_enabled and not fitted_next:
        raise HTTPException(status_code=409, detail="Say the Blinkt strip is fitted first")
    if body.radio_enabled and fitted_next:
        raise HTTPException(
            status_code=409,
            detail="A Blinkt strip is marked as fitted. Remove it and turn that setting off first",
        )
    if body.leds_fitted and await asyncio.to_thread(_radio_active):
        raise HTTPException(
            status_code=409,
            detail="The DABBoard radio is on, and it shares GPIO 23 with the Blinkt strip",
        )
    if body.leds_enabled or body.radio_enabled:
        leds_next = _leds_enabled if body.leds_enabled is None else body.leds_enabled
        radio_saved = await asyncio.to_thread(_radio_active)
        radio_next = radio_saved if body.radio_enabled is None else (
            body.radio_enabled or await asyncio.to_thread(radio_overlay_configured)
        )
        if leds_next and radio_next:
            raise HTTPException(
                status_code=409,
                detail="The Blinkt LEDs and the DABBoard radio share GPIO 23; turn one off first",
            )

    # Everything that can be refused is checked before the first write: a 400
    # after half the request was saved left the UI and the box disagreeing.
    if body.autoconnect_strategy is not None and body.autoconnect_strategy not in ("strongest", "primary"):
        raise HTTPException(status_code=400, detail="strategy must be 'strongest' or 'primary'")
    if body.radio_rds_region is not None and body.radio_rds_region not in ("E", "U"):
        raise HTTPException(status_code=400, detail="region must be E (RDS) or U (RBDS)")
    unknown_modules = [name for name in (body.modules or {}) if name not in MODULES]
    if unknown_modules:
        raise HTTPException(status_code=400, detail=f"unknown module: {unknown_modules[0]}")
    clean_recording: dict = {}
    if body.recording:
        from backend.services.recorder import RecorderError, validate as validate_recording

        try:
            clean_recording = validate_recording(body.recording)
        except RecorderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _apply() -> None:
        if body.resume_on_boot is not None:
            cfg_set("audio.resume_on_boot", "true" if body.resume_on_boot else "false")
        if body.autoconnect_strategy is not None:
            cfg_set("bluetooth.autoconnect_strategy", body.autoconnect_strategy)
        if body.wedge_reboot is not None:
            cfg_set("bluetooth.wedge_reboot", "true" if body.wedge_reboot else "false")
        if body.radio_enabled is not None:
            get_radio().set_enabled(body.radio_enabled)
        if body.radio_resume_on_boot is not None:
            cfg_set("radio.resume_on_boot", "true" if body.radio_resume_on_boot else "false")
        if body.radio_rds_region is not None:
            get_radio().set_region(body.radio_rds_region)

    try:
        await asyncio.to_thread(_apply)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if body.leds_fitted is not None:
        await asyncio.to_thread(cfg_set, "leds.fitted", "true" if body.leds_fitted else "false")
        if not body.leds_fitted:
            _leds_enabled = False
            await asyncio.to_thread(cfg_set, "leds.enabled", "false")
    if body.leds_enabled is not None:
        _leds_enabled = body.leds_enabled
        await asyncio.to_thread(cfg_set, "leds.enabled", "true" if body.leds_enabled else "false")
    if body.leds_brightness is not None:
        _leds_brightness = max(0.01, min(1.0, body.leds_brightness))
        await asyncio.to_thread(cfg_set, "leds.brightness", str(round(_leds_brightness, 3)))

    if body.source_enabled:
        known = set(sources_svc.PROVIDERS.keys())

        def _set_sources() -> None:
            for sid, on in body.source_enabled.items():
                if sid in known:
                    cfg_set(f"sources.{sid}.enabled", "true" if on else "false")

        await asyncio.to_thread(_set_sources)
    if body.modules:
        for name, on in body.modules.items():
            await asyncio.to_thread(cfg_set, f"modules.{name}", "true" if on else "false")
            if name == "links":
                # Nothing of Web links runs while idle except the weekly yt-dlp
                # upgrade; off means that stops too.
                await asyncio.to_thread(
                    subprocess.run,
                    ["systemctl", "enable" if on else "disable", "--now", "audio-player-ytdlp-update.timer"],
                    capture_output=True,
                    timeout=30,
                )
    for key, value in clean_recording.items():
        await asyncio.to_thread(cfg_set, f"recording.{key}", str(value))
    if body.rewind_minutes is not None:
        minutes = max(0, min(120, int(body.rewind_minutes)))
        await asyncio.to_thread(cfg_set, "audio.rewind_minutes", str(minutes))
        # Re-size the buffer of whatever is playing now, not only the next one.
        await _run_player(get_player().apply_rewind)
    if body.jamendo_client_id is not None:
        await asyncio.to_thread(cfg_set, "sources.jamendo.client_id", body.jamendo_client_id.strip())

    return await get_config()


@app.post("/api/play/resume")
async def play_resume() -> dict:
    """Resume the saved source if mpv sits idle (install.sh calls this at the
    end of a deploy when something was playing before it began; the install
    restarts mpv after the backend's own resume-on-boot has already run).
    Waits up to 30 s for the mpv socket. A saved FM session is handed to the
    radio worker, which plays it once the speaker is ready."""
    if get_player().saved_state().get("source") == "radio":
        return {"resumed": get_radio().resume_interrupted(), "source": "radio"}
    resumed = await _resume_if_idle(wait_mpv=30)
    return {"resumed": resumed, "source": get_player().saved_state().get("source")}


@app.post("/api/play/stream/{stream_id}")
async def play_stream(stream_id: str) -> dict:
    result = await _run_player(get_player().play_stream, stream_id)
    await _ensure_viz_for_clients()
    return result


@app.post("/api/play/url")
async def play_url(body: PlayUrlBody) -> dict:
    meta = {"name": body.name, "genre": body.genre} if body.name else {}
    if body.live is not None:
        meta["is_live"] = body.live
    result = await _run_player(get_player().play_url, body.url, meta or None)
    await _ensure_viz_for_clients()
    return result


@app.get("/api/links")
async def list_links() -> dict:
    links = get_links()
    return {
        "ytdl": ytdl_path() is not None,
        "ytdl_version": await asyncio.to_thread(links.version),
        "platforms": PLATFORM_LABELS,
        "links": links.list(),
        "cookies": links.cookies_status(),
        "playlists": links.playlists(),
    }


@app.post("/api/links/playlists", dependencies=[Depends(require_token)])
async def create_link_playlist(body: PlaylistCreateBody) -> dict:
    try:
        return await asyncio.to_thread(get_links().create_playlist, body.name)
    except LinkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _playing_playlist_id() -> str | None:
    saved = get_player().saved_state()
    if saved.get("source") != "link_playlist":
        return None
    return (saved.get("meta") or {}).get("playlist_id")


@app.put("/api/links/playlists/{playlist_id}", dependencies=[Depends(require_token)])
async def update_link_playlist(playlist_id: str, body: PlaylistUpdateBody) -> dict:
    try:
        playlist = await asyncio.to_thread(
            get_links().update_playlist,
            playlist_id,
            name=body.name,
            link_ids=body.link_ids,
            shuffle=body.shuffle,
            loop=body.loop,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown playlist: {playlist_id}") from exc
    except LinkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Edits to the playlist that is playing apply to mpv's queue right away,
    # without interrupting the current entry.
    if _playing_playlist_id() == playlist_id:
        links = await asyncio.to_thread(get_links().playlist_links, playlist_id)
        if body.shuffle is not None or body.loop is not None:
            await _run_player(
                get_player().set_playlist_modes, links, shuffle=body.shuffle, loop=body.loop
            )
        if body.link_ids is not None:
            # Shuffled: membership only — the order is random on purpose.
            await _run_player(get_player().sync_playlist, links, reorder=not playlist["shuffle"])
    return playlist


@app.delete("/api/links/playlists/{playlist_id}", dependencies=[Depends(require_token)])
async def delete_link_playlist(playlist_id: str) -> dict:
    if not await asyncio.to_thread(get_links().delete_playlist, playlist_id):
        raise HTTPException(status_code=404, detail=f"unknown playlist: {playlist_id}")
    return {"ok": True, "removed": playlist_id}


@app.post("/api/play/link-playlist/{playlist_id}")
async def play_link_playlist(playlist_id: str, start: int = Query(default=0)) -> dict:
    links = get_links()
    playlist = links.get_playlist(playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail=f"unknown playlist: {playlist_id}")
    meta = {"name": playlist["name"], "genre": "Playlist", "playlist_id": playlist_id}
    result = await _run_player(
        get_player().play_link_playlist,
        links.playlist_links(playlist_id),
        meta,
        start=start,
        shuffle=playlist["shuffle"],
        loop=playlist["loop"],
    )
    await _ensure_viz_for_clients()
    return result


@app.put("/api/links/cookies/{platform}", dependencies=[Depends(require_token)])
async def set_link_cookies(platform: str, body: CookiesBody) -> dict:
    try:
        return await asyncio.to_thread(get_links().set_cookies, platform, body.content)
    except LinkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/links/cookies/{platform}", dependencies=[Depends(require_token)])
async def delete_link_cookies(platform: str) -> dict:
    try:
        return await asyncio.to_thread(get_links().delete_cookies, platform)
    except LinkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/links", dependencies=[Depends(require_token)])
async def add_link(body: LinkBody) -> dict:
    try:
        return await asyncio.to_thread(get_links().add, body.url)
    except LinkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/links/{link_id}", dependencies=[Depends(require_token)])
async def delete_link(link_id: str) -> dict:
    if not await asyncio.to_thread(get_links().delete, link_id):
        raise HTTPException(status_code=404, detail=f"unknown link: {link_id}")
    return {"ok": True, "removed": link_id}


@app.post("/api/play/link/{link_id}")
async def play_link(link_id: str) -> dict:
    link = get_links().get(link_id)
    if not link:
        raise HTTPException(status_code=404, detail=f"unknown link: {link_id}")
    meta = {
        "name": link.get("title"),
        "genre": PLATFORM_LABELS.get(link.get("platform", ""), "Web"),
        "link_id": link_id,
        "platform": link.get("platform"),
        "is_live": bool(link.get("is_live")),
    }
    result = await _run_player(get_player().play_link, link["url"], meta)
    await _ensure_viz_for_clients()
    return result


@app.post("/api/play/file")
async def play_file(body: PlayFileBody) -> dict:
    result = await _run_player(get_player().play_file, body.path)
    await _ensure_viz_for_clients()
    return result


@app.post("/api/transport/pause")
async def transport_pause() -> dict:
    return await _run_player(get_player().pause, True)


@app.post("/api/transport/resume")
async def transport_resume() -> dict:
    return await _run_player(get_player().pause, False)


@app.post("/api/transport/toggle")
async def transport_toggle() -> dict:
    return await _run_player(get_player().toggle_pause)


@app.post("/api/transport/seek")
async def transport_seek(body: SeekBody) -> dict:
    return await _run_player(get_player().seek, body.position)


def _stream_settings_payload() -> dict:
    return {
        **stream_settings.read(cfg_get),
        "defaults": stream_settings.defaults(),
        "retries_supported": stream_settings.ffmpeg_has_max_retries(),
    }


@app.get("/api/stream-settings")
async def get_stream_settings() -> dict:
    return await asyncio.to_thread(_stream_settings_payload)


@app.put("/api/stream-settings", dependencies=[Depends(require_token)])
async def put_stream_settings(body: StreamSettingsBody) -> dict:
    raw = _dump(body)
    reset = raw.pop("reset")
    patch = stream_settings.defaults() if reset else {k: v for k, v in raw.items() if v is not None}
    try:
        clean = stream_settings.validate(patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _save() -> dict:
        for key, value in clean.items():
            cfg_set(stream_settings.CFG_KEY[key], stream_settings.to_yaml_value(value))
        return stream_settings.read(cfg_get)

    current = await asyncio.to_thread(_save)
    # Live, so nothing needs a restart (see mpv_properties for what applies when).
    await _run_player(get_player().apply_stream_settings, current)
    return await asyncio.to_thread(_stream_settings_payload)


# ── recordings ────────────────────────────────────────────────────────
class RecordingRenameBody(BaseModel):
    name: str = ""


def _recorder():
    return get_player().recorder


async def _recorder_call(fn, *args):
    from backend.services.recorder import RecorderError

    try:
        return await asyncio.to_thread(fn, *args)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown recording") from exc
    except RecorderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/recordings")
async def list_recordings() -> dict:
    rec = _recorder()

    def _read() -> dict:
        try:
            target = str(rec.target_dir())
        except ValueError:
            target = None
        return {
            "status": rec.status(),
            "items": rec.list(),
            "settings": rec.settings(),
            "target_dir": target,
            "usb_available": rec.usb_dir() is not None,
            "space": rec.free_space(),
        }

    return await asyncio.to_thread(_read)


@app.post("/api/recordings/start", dependencies=[Depends(require_token)])
async def start_recording() -> dict:
    return await _recorder_call(_recorder().start)


@app.post("/api/recordings/stop", dependencies=[Depends(require_token)])
async def stop_recording() -> dict:
    return await _recorder_call(_recorder().stop)


@app.put("/api/recordings/{rec_id}", dependencies=[Depends(require_token)])
async def rename_recording(rec_id: str, body: RecordingRenameBody) -> dict:
    return await _recorder_call(_recorder().rename, rec_id, body.name)


@app.delete("/api/recordings/{rec_id}", dependencies=[Depends(require_token)])
async def delete_recording(rec_id: str) -> dict:
    await _recorder_call(_recorder().delete, rec_id)
    return {"ok": True}


@app.get("/api/recordings/{rec_id}/download")
async def download_recording(rec_id: str):
    path = await _recorder_call(_recorder().resolve, rec_id)
    return FileResponse(path, filename=path.name)


@app.post("/api/play/recording/{rec_id}")
async def play_recording(rec_id: str) -> dict:
    path = await _recorder_call(_recorder().resolve, rec_id)
    result = await _run_player(get_player().play_recording, path, {"name": path.stem})
    await _ensure_viz_for_clients()
    return result


@app.post("/api/transport/next")
async def transport_next() -> dict:
    return await _run_player(get_player().playlist_step, True)


@app.post("/api/transport/prev")
async def transport_prev() -> dict:
    return await _run_player(get_player().playlist_step, False)


@app.post("/api/transport/live")
async def transport_live() -> dict:
    return await _run_player(get_player().go_live)


@app.post("/api/transport/stop")
async def transport_stop() -> dict:
    return await _run_player(get_player().stop)


@app.put("/api/volume")
async def set_volume(body: VolumeBody) -> dict:
    return await _run_player(get_player().set_volume, body.level)


@app.put("/api/volume/follow")
async def set_volume_follow(body: VolumeFollowBody) -> dict:
    return await _run_player(get_player().set_volume_follow, body.enabled)


@app.get("/api/usb")
async def list_usb() -> list[dict]:
    # In a thread: without a cache this is a full recursive scan of the stick,
    # which on the event loop stalled every request and the VU stream with it.
    return await asyncio.to_thread(get_player().list_usb)


@app.post("/api/usb/scan")
async def scan_usb() -> list[dict]:
    return await _run_player(get_player().scan_usb)


@app.get("/api/files/sources")
async def files_sources() -> list[dict]:
    return await asyncio.to_thread(files_svc.list_sources)


@app.get("/api/files/browse")
async def files_browse(source: str = Query(...), path: str = Query(default="")) -> dict:
    try:
        return await asyncio.to_thread(files_svc.browse, source, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/files/drives", dependencies=[Depends(require_token)])
async def files_add_drive(body: NetworkDriveBody) -> dict:
    try:
        return await asyncio.to_thread(files_svc.add_network_drive, _dump(body))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/files/drives/{drive_id}/mount", dependencies=[Depends(require_token)])
async def files_mount_drive(drive_id: str) -> dict:
    try:
        result = await asyncio.to_thread(files_svc.mount_drive, drive_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("detail") or "mount failed")
    return result


@app.delete("/api/files/drives/{drive_id}", dependencies=[Depends(require_token)])
async def files_remove_drive(drive_id: str) -> dict:
    try:
        return await asyncio.to_thread(files_svc.remove_network_drive, drive_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/eq")
async def eq_status() -> dict:
    return get_player().eq.active_preset()


@app.put("/api/eq/enabled")
async def eq_enabled(body: EqBody) -> dict:
    player = get_player()
    await _run_player(player.eq.set_enabled, body.enabled)
    await _run_player(player.refresh_audio_filters)
    return player.eq.active_preset()


@app.put("/api/eq/preset/{preset_id}")
async def eq_preset(preset_id: str) -> dict:
    player = get_player()
    await _run_player(player.eq.set_preset, preset_id)
    await _run_player(player.refresh_audio_filters)
    return player.eq.active_preset()


@app.put("/api/eq/custom")
async def eq_custom(body: EqBandsBody) -> dict:
    player = get_player()
    await _run_player(player.eq.set_custom_bands, body.bands)
    await _run_player(player.refresh_audio_filters)
    return player.eq.active_preset()


@app.get("/api/eq/presets")
async def eq_presets() -> list[dict]:
    return get_player().eq.list_presets()


@app.get("/api/bt/status")
async def bt_status(refresh: bool = Query(default=False)) -> dict:
    if refresh:
        return await asyncio.to_thread(refresh_bt_status)
    # to_thread: read_bt_status may run a blocking bluetoothctl reconcile
    return await asyncio.to_thread(read_bt_status)


@app.post("/api/bt/connect")
async def bt_connect_endpoint(body: BtConnectBody | None = None) -> dict:
    mac = body.mac if body else None
    result = await asyncio.to_thread(bt_connect, mac)
    if result.get("ok") and (result.get("bt") or {}).get("connected"):
        result["resumed"] = await _resume_if_idle()
    return result


@app.get("/api/bt/devices")
async def bt_devices() -> list[dict]:
    return await asyncio.to_thread(list_devices)


@app.post("/api/bt/scan")
async def bt_scan(seconds: int = Query(default=12, ge=5, le=60)) -> dict:
    return await asyncio.to_thread(scan_devices, seconds)


@app.post("/api/bt/scan/start")
async def bt_scan_start(seconds: int = Query(default=25, ge=5, le=60)) -> dict:
    return await asyncio.to_thread(start_scan, seconds)


@app.post("/api/bt/pair", dependencies=[Depends(require_token)])
async def bt_pair_endpoint(body: BtMacBody) -> dict:
    result = await asyncio.to_thread(pair_device, body.mac.strip())
    if not result.get("ok"):
        raise HTTPException(
            status_code=500,
            detail=result.get("stderr") or result.get("stdout") or "pair failed",
        )
    return result


@app.post("/api/bt/unpair", dependencies=[Depends(require_token)])
async def bt_unpair_endpoint(body: BtMacBody) -> dict:
    result = await asyncio.to_thread(remove_device, body.mac.strip())
    if not result.get("ok"):
        raise HTTPException(
            status_code=500,
            detail=result.get("stderr") or result.get("detail") or "unpair failed",
        )
    return result


@app.post("/api/bt/codec", dependencies=[Depends(require_token)])
async def bt_codec_endpoint(body: BtCodecBody) -> dict:
    result = await asyncio.to_thread(select_codec, body.codec.strip(), body.mac)
    resumed = await _resume_if_idle()
    if not result.get("ok"):
        raise HTTPException(
            status_code=500,
            detail=result.get("stderr") or result.get("detail") or "codec switch failed",
        )
    result["resumed"] = resumed
    return result


@app.post("/api/bt/primary", dependencies=[Depends(require_token)])
async def bt_primary_endpoint(body: BtMacBody) -> dict:
    result = await asyncio.to_thread(set_primary, body.mac.strip())
    if not result.get("ok"):
        raise HTTPException(
            status_code=500,
            detail=result.get("stderr") or result.get("detail") or "set primary failed",
        )
    result["resumed"] = await _resume_if_idle(wait_mpv=12)
    return result


@app.get("/api/audio/verify")
async def audio_verify(
    test_tone: bool = Query(default=False),
    require_playback: bool = Query(default=False),
) -> dict:
    return await _run_player(
        get_player().verify,
        test_tone=test_tone,
        require_playback=require_playback,
    )


@app.get("/api/network/status")
async def network_status() -> dict:
    return await asyncio.to_thread(read_network_status)


@app.get("/api/network/wifi/scan")
async def network_wifi_scan() -> dict:
    return await asyncio.to_thread(scan_wifi)


@app.post("/api/network/wifi", dependencies=[Depends(require_token)])
async def network_add_wifi(body: WifiBody) -> dict:
    if not body.ssid.strip():
        raise HTTPException(status_code=400, detail="ssid required")
    result = await asyncio.to_thread(add_wifi, body.ssid.strip(), body.password)
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("stderr") or "failed to save WiFi")
    return result


@app.post("/api/network/captive/start", dependencies=[Depends(require_token)])
async def network_captive_start() -> dict:
    result = await asyncio.to_thread(captive_start)
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("detail", "captive start failed"))
    return result


@app.post("/api/network/captive/stop", dependencies=[Depends(require_token)])
async def network_captive_stop() -> dict:
    return await asyncio.to_thread(captive_stop)


@app.delete("/api/network/wifi/{network_id}", dependencies=[Depends(require_token)])
async def network_forget_wifi(network_id: str) -> dict:
    if not network_id.strip():
        raise HTTPException(status_code=400, detail="network id required")
    result = await asyncio.to_thread(forget_wifi, network_id.strip())
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("stderr") or "forget failed")
    return result


@app.get("/api/radio")
async def radio_status() -> dict:
    return await asyncio.to_thread(get_radio().status)


@app.post("/api/radio/play")
async def radio_play(body: RadioPlayBody) -> dict:
    result = await _run_player(get_radio().play, body.frequency_khz)
    await _ensure_viz_for_clients()
    return result


@app.post("/api/radio/tune")
async def radio_tune(body: RadioTuneBody) -> dict:
    return await _run_player(get_radio().tune, body.frequency_khz)


@app.post("/api/radio/seek")
async def radio_seek(body: RadioSeekBody) -> dict:
    return await _run_player(get_radio().seek, body.up)


@app.post("/api/radio/mono")
async def radio_mono(body: RadioMonoBody) -> dict:
    return await _run_player(get_radio().set_mono, body.enabled)


@app.post("/api/radio/stop")
async def radio_stop() -> dict:
    return await _run_player(get_radio().stop)


@app.post("/api/radio/scan")
async def radio_scan() -> dict:
    return await _run_player(get_radio().start_scan)


@app.post("/api/radio/presets")
async def radio_add_preset(body: RadioPresetBody) -> dict:
    return await _run_player(get_radio().add_preset, body.name, body.frequency_khz)


@app.delete("/api/radio/presets/{frequency_khz}")
async def radio_delete_preset(frequency_khz: int) -> dict:
    return await _run_player(get_radio().delete_preset, frequency_khz)


class HostnameBody(BaseModel):
    name: str = ""
    force: bool = False


# ── optional web login (nginx basic auth; scripts/web_auth.sh) ───────────
class AuthBody(BaseModel):
    enabled: bool
    username: str | None = None
    # None/"" = keep the current password ("safydo" when there is none yet).
    password: str | None = None


def _web_auth(*args: str, password: str = "") -> dict:
    from backend.services.scripts import TIMEOUT_RC, last_error, run_script

    # The password goes on stdin, never into argv (visible in ps).
    proc = run_script("web_auth.sh", *args, timeout=30, input=password + "\n" if password else "")
    if proc.returncode != 0:
        status = 504 if proc.returncode == TIMEOUT_RC else 400
        raise HTTPException(status_code=status, detail=last_error(proc, "web login change failed"))
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise HTTPException(status_code=500, detail="web login: unexpected script output") from exc


@app.get("/api/auth")
async def auth_status() -> dict:
    return await asyncio.to_thread(_web_auth, "status")


@app.put("/api/auth", dependencies=[Depends(require_token)])
async def auth_set(body: AuthBody) -> dict:
    password = body.password or ""
    if "\n" in password or "\r" in password:
        raise HTTPException(status_code=400, detail="password must be one line")
    if len(password) > 128:
        raise HTTPException(status_code=400, detail="password: at most 128 characters")
    if body.enabled:
        args = ["enable"] + ([body.username.strip()] if body.username and body.username.strip() else [])
        result = await asyncio.to_thread(_web_auth, *args, password=password)
    else:
        result = await asyncio.to_thread(_web_auth, "disable")
    return result


@app.post("/api/system/hostname", dependencies=[Depends(require_token)])
async def system_hostname(body: HostnameBody) -> dict:
    from backend.services.system import set_hostname

    try:
        return await asyncio.to_thread(set_hostname, body.name, body.force)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/system/reboot", dependencies=[Depends(require_token)])
async def system_reboot() -> dict:
    subprocess.Popen(["systemctl", "reboot"], start_new_session=True)
    return {"status": "rebooting"}


@app.post("/api/system/shutdown", dependencies=[Depends(require_token)])
async def system_shutdown() -> dict:
    script = INSTALL_DIR / "scripts" / "safe_shutdown.sh"
    if not script.is_file():
        raise HTTPException(status_code=500, detail="shutdown script missing")
    subprocess.Popen([str(script)], start_new_session=True)
    return {"status": "shutting_down"}


@app.get("/api/system/resources")
async def system_resources() -> dict:
    from backend.services import resources

    return await asyncio.to_thread(resources.snapshot, str(cfg_get("paths.usb_mount") or "/media/usb"))


@app.get("/api/system/diag", response_class=PlainTextResponse)
async def system_diag() -> str:
    """Diagnostic snapshot (BT/audio/network state + recent logs) for debugging
    playback/BT drop-outs. Runs scripts/diag.sh (backend is root) and returns it."""
    script = INSTALL_DIR / "scripts" / "diag.sh"
    if not script.is_file():
        raise HTTPException(status_code=500, detail="diag script missing")

    def _run() -> str:
        proc = subprocess.run(
            [str(script), "--stdout"], capture_output=True, text=True, errors="replace", timeout=40, check=False
        )
        return proc.stdout or proc.stderr or "(no diagnostic output)"

    return await asyncio.to_thread(_run)


@app.websocket("/ws/status")
async def ws_status(websocket: WebSocket) -> None:
    global _status_task
    await websocket.accept()
    _ws_clients.add(websocket)
    if _status_task is None or _status_task.done():
        _status_task = asyncio.create_task(_broadcast_status())
    try:
        player = get_player()

        def _initial_payload() -> dict:
            player.set_viz_active(True)
            player.spectrum.tick(analyze=True)
            payload = full_status(player, deep_check=False)
            payload["spectrum"] = {"bands": player.spectrum.levels(), "active": True}
            return payload

        await websocket.send_text(
            json.dumps(await asyncio.to_thread(_initial_payload))
        )
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)
#!/usr/bin/env python3
"""SAFYDO terminal UI — control the player over SSH, in sync with the web UI.

Talks to the same FastAPI backend (WebSocket for live status + stereo VU, REST
for actions), so it's automatically in sync with the browser. Read-only for the
station library (no add/edit) — browse, play, pause, stop, volume.

    python3 tui.py            # (on the Pi, or set SAFYDO_URL for remote)

Keys:  ↑/↓ or j/k select · Enter play · Space pause · s stop
       ←/→ or -/+ volume · f favorites · r refresh · q quit

Deps: rich (apt install python3-rich), websockets (ships with the backend).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import termios
import tty
import urllib.request

try:
    import websockets
except Exception:  # pragma: no cover
    print("Missing 'websockets' (pip/apt install python3-websockets)", file=sys.stderr)
    sys.exit(1)

try:
    from rich.console import Console, Group
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except Exception:  # pragma: no cover
    print("Missing 'rich' — install it:  sudo apt install -y python3-rich", file=sys.stderr)
    sys.exit(1)

BASE = os.environ.get("SAFYDO_URL", "http://127.0.0.1:8000").rstrip("/")
WS = BASE.replace("http", "ws", 1) + "/ws/status"
ACCENT = "#6c5ce7"


GENRE_COLORS = {
    "HOUSE": "#ff6b35", "DANCE": "#f72585", "CHILL": "#4ecdc4", "JAZZ": "#c9a227",
    "ROCK": "#e63946", "AC": "#9b5de5", "CHR": "#00bbf9", "TALK": "#90be6d",
    "PSY": "#b5179e", "DNB": "#7209b7", "REGGAE": "#2a9d8f", "TOP40": "#fee440",
}


def genre_color(g: str) -> str:
    return GENRE_COLORS.get((g or "").strip().upper(), ACCENT)


class State:
    def __init__(self) -> None:
        self.status: dict = {}
        self.vu: list[float] = [0.0, 0.0]
        self.streams: list[dict] = []
        self.connected = False
        self.sel = 0
        self.favorites = False
        self.msg = ""
        self.running = True

    def view(self) -> list[dict]:
        return [s for s in self.streams if s.get("favorite")] if self.favorites else self.streams


st = State()


# ---- REST helpers (run in a thread; localhost, fast) -------------------------
def _req(path: str, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


async def api(path: str, method: str = "GET", body: dict | None = None):
    try:
        return await asyncio.to_thread(_req, path, method, body)
    except Exception as exc:
        st.msg = f"! {type(exc).__name__}"
        return None


# ---- rendering ---------------------------------------------------------------
def bar(level: float, width: int, dim: str = "grey15") -> Text:
    level = max(0.0, min(1.0, level))
    filled = int(round(level * width))
    t = Text()
    for i in range(width):
        if i < filled:
            frac = i / max(1, width - 1)
            color = "green3" if frac < 0.6 else ("gold1" if frac < 0.82 else "red1")
            t.append("█", style=color)
        else:
            t.append("─", style=dim)
    return t


def header() -> Panel:
    pl = st.status.get("player", {})
    net = st.status.get("hostname") or "safydo"
    dot = Text(" ● Live ", style="bold green3") if st.connected else Text(" ○ offline ", style="grey54")
    title = Text.assemble(("SAFYDO", f"bold {ACCENT}"), ("  terminal", "grey54"))
    line = Table.grid(expand=True)
    line.add_column(justify="left")
    line.add_column(justify="right")
    line.add_row(title, dot)
    return Panel(line, style="grey42", padding=(0, 1))


def now_playing() -> Panel:
    mpv = st.status.get("player", {}).get("mpv", {})
    saved = st.status.get("player", {}).get("saved", {})
    info = mpv.get("track_info") or {}
    idle = mpv.get("idle", True) or not mpv.get("running")
    paused = mpv.get("paused")
    title = info.get("title") or mpv.get("title") or (saved.get("meta") or {}).get("name") or "Ready"
    state = "■ stopped" if idle else ("⏸ paused" if paused else "▶ playing")
    genre = (saved.get("meta") or {}).get("genre") or ""
    src = saved.get("source") or ""
    sub = "Nothing playing" if idle else (genre or src or "")

    chips = Text()
    if not idle:
        if info.get("bitrate_kbps"):
            chips.append(f" {info['bitrate_kbps']} kbps ", style=f"on grey19")
        if info.get("format"):
            chips.append(f" {info['format']} ", style="on grey19")
        if genre:
            chips.append(f" {genre} ", style=f"on {genre_color(genre)}")

    g = Group(
        Text(title[:70], style="bold white"),
        Text(sub[:70], style="grey62"),
        chips,
    )
    return Panel(g, title=Text(state, style=ACCENT if not idle else "grey54"),
                 title_align="left", style="grey42", padding=(0, 1))


def vu_panel(width: int) -> Panel:
    lo, hi = (st.vu + [0.0, 0.0])[:2]
    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="grey62", width=1)
    grid.add_column()
    w = max(10, width - 8)
    grid.add_row("L", bar(lo, w))
    grid.add_row("R", bar(hi, w))
    return Panel(grid, title=Text("VU", style="grey54"), title_align="left", style="grey42", padding=(0, 1))


def volume_line() -> Panel:
    pl = st.status.get("player", {})
    mpv = pl.get("mpv", {})
    vol = int(round(mpv.get("volume") or pl.get("volume_config") or 0))
    bt = st.status.get("bluetooth", {})
    dev = bt.get("device") or "—"
    codec = bt.get("codec") or ""
    conn = "🔊" if bt.get("connected") else "🔇"
    t = Table.grid(expand=True)
    t.add_column(justify="left")
    t.add_column(justify="right")
    volbar = bar(vol / 100, 20)
    left = Text.assemble(("vol ", "grey54"), volbar, (f" {vol:>3}%", "white"))
    right = Text(f"{conn} {dev} {codec}", style="grey62")
    t.add_row(left, right)
    return Panel(t, style="grey42", padding=(0, 1))


def streams_panel(height: int) -> Panel:
    view = st.view()
    saved = st.status.get("player", {}).get("saved", {})
    now_url = saved.get("url")
    rows = max(3, height - 2)
    if st.sel >= len(view):
        st.sel = max(0, len(view) - 1)
    start = min(max(0, st.sel - rows // 2), max(0, len(view) - rows))
    tbl = Table.grid(expand=True, padding=(0, 1))
    tbl.add_column(width=2)
    tbl.add_column(ratio=1)
    tbl.add_column(justify="right", style="grey42")
    for i in range(start, min(start + rows, len(view))):
        s = view[i]
        playing = now_url and s.get("url") == now_url
        selected = i == st.sel
        mark = "▶" if playing else ("›" if selected else " ")
        name = Text(s.get("name", "")[:48], style=("bold white" if selected else "grey78"))
        gcol = genre_color(s.get("genre", ""))
        gid = Text(f" {s.get('genre','')} ", style=f"on {gcol}" if selected else gcol)
        if selected:
            name.stylize("reverse")
        tbl.add_row(Text(mark, style=ACCENT), name, gid)
    title = Text.assemble(("Stations ", "grey54"), (f"{len(view)}", "white"),
                          (" ★ favorites" if st.favorites else "", "gold1"))
    return Panel(tbl, title=title, title_align="left", style="grey42", padding=(0, 0))


def footer() -> Panel:
    keys = ("[white]↑↓[/] select  [white]⏎[/] play  [white]space[/] pause  "
            "[white]s[/] stop  [white]←→[/] vol  [white]f[/] fav  [white]r[/] refresh  [white]q[/] quit")
    line = Table.grid(expand=True)
    line.add_column(justify="left")
    line.add_column(justify="right")
    line.add_row(Text.from_markup(keys), Text(st.msg, style="grey54"))
    return Panel(line, style="grey30", padding=(0, 1))


def build(console: Console) -> Layout:
    h = console.size.height
    lay = Layout()
    lay.split_column(
        Layout(header(), name="h", size=3),
        Layout(now_playing(), name="np", size=6),
        Layout(vu_panel(console.size.width), name="vu", size=4),
        Layout(volume_line(), name="vol", size=3),
        Layout(name="st"),
        Layout(footer(), name="ft", size=3),
    )
    lay["st"].update(streams_panel(h - 19))
    return lay


# ---- input -------------------------------------------------------------------
def parse_keys(data: str) -> list[str]:
    keys, i = [], 0
    while i < len(data):
        c = data[i]
        if c == "\x1b" and data[i + 1 : i + 2] == "[":
            keys.append({"A": "up", "B": "down", "C": "right", "D": "left"}.get(data[i + 2 : i + 3], "esc"))
            i += 3
        else:
            keys.append(c)
            i += 1
    return keys


async def do_volume(delta: int) -> None:
    pl = st.status.get("player", {})
    cur = int(round(pl.get("mpv", {}).get("volume") or pl.get("volume_config") or 50))
    await api("/api/volume", "PUT", {"level": max(0, min(100, cur + delta))})


async def handle(key: str) -> None:
    view = st.view()
    if key in ("q", "\x03"):
        st.running = False
    elif key in ("up", "k"):
        st.sel = max(0, st.sel - 1)
    elif key in ("down", "j"):
        st.sel = min(len(view) - 1, st.sel + 1)
    elif key in ("\r", "\n"):
        if view:
            s = view[st.sel]
            st.msg = f"▶ {s['name'][:30]}"
            await api(f"/api/play/stream/{s['id']}", "POST")
    elif key == " ":
        await api("/api/transport/toggle", "POST")
    elif key == "s":
        await api("/api/transport/stop", "POST")
    elif key in ("right", "+", "="):
        await do_volume(+5)
    elif key in ("left", "-", "_"):
        await do_volume(-5)
    elif key == "f":
        st.favorites = not st.favorites
        st.sel = 0
    elif key == "r":
        data = await api("/api/streams")
        if data is not None:
            st.streams = data
            st.msg = f"{len(data)} stations"


async def ws_reader() -> None:
    while st.running:
        try:
            async with websockets.connect(WS, open_timeout=6, ping_interval=20) as ws:
                st.connected = True
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") == "spectrum":
                        if isinstance(msg.get("bands"), list):
                            st.vu = msg["bands"]
                    else:
                        st.status = msg
                        spec = msg.get("spectrum") or {}
                        if isinstance(spec.get("bands"), list):
                            st.vu = spec["bands"]
        except Exception:
            pass
        # A clean close (backend restart, or the server refusing the socket)
        # ends `async for` without raising: back off on that path too, or it
        # reconnected in a tight loop still showing "Live".
        st.connected = False
        if st.running:
            await asyncio.sleep(1.0)


async def render_loop(console: Console) -> None:
    with Live(build(console), console=console, screen=True, refresh_per_second=30, transient=True) as live:
        while st.running:
            live.update(build(console))
            await asyncio.sleep(0.04)


async def main() -> None:
    console = Console()
    data = await api("/api/streams")
    st.streams = data or []
    st.msg = f"{len(st.streams)} stations · {BASE}"

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    loop = asyncio.get_running_loop()

    def on_input() -> None:
        try:
            data = os.read(fd, 64).decode(errors="ignore")
        except OSError:
            return
        for k in parse_keys(data):
            loop.create_task(handle(k))

    loop.add_reader(fd, on_input)
    with contextlib.suppress(NotImplementedError, ValueError):
        loop.add_signal_handler(signal.SIGINT, lambda: setattr(st, "running", False))

    # render_loop returns as soon as st.running goes False (q / Ctrl-C). The WS
    # reader blocks awaiting frames, so cancel it explicitly — otherwise the
    # program would hang after quitting and never restore the terminal.
    ws_task = asyncio.create_task(ws_reader())
    try:
        await render_loop(console)
    finally:
        st.running = False
        ws_task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(ws_task, timeout=2)  # bounded — never hang on exit
        loop.remove_reader(fd)
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.remove_signal_handler(signal.SIGINT)
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


async def check() -> None:
    """Non-interactive smoke test: load streams, read a few WS frames, render
    one frame off-screen. Validates deps + backend connectivity without a TTY."""
    import io

    console = Console(file=io.StringIO(), width=90, height=30)
    data = await api("/api/streams")
    st.streams = data or []
    try:
        async with websockets.connect(WS, open_timeout=6) as ws:
            st.connected = True
            for _ in range(4):
                msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
                if msg.get("type") == "spectrum":
                    st.vu = msg.get("bands", st.vu)
                else:
                    st.status = msg
    except Exception as exc:
        print("WS error:", exc)
    console.print(build(console))
    out = console.file.getvalue()
    print(f"CHECK OK  streams={len(st.streams)}  ws_connected={st.connected}  "
          f"vu={st.vu}  status_keys={sorted(st.status.keys())[:6]}  render_lines={out.count(chr(10))}")


if __name__ == "__main__":
    try:
        if "--check" in sys.argv:
            asyncio.run(check())
        else:
            asyncio.run(main())
    except KeyboardInterrupt:
        pass

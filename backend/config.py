"""Runtime config + service wiring for the FastAPI backend."""

from __future__ import annotations

import importlib.util
import threading
from functools import lru_cache
from pathlib import Path

from backend.services.links import LinksService
from backend.services.player import PlayerService
from backend.services.recorder import Recorder
from backend.services.radio import RadioService

INSTALL_DIR = Path("/opt/rpi-audio-player")


def _load_yaml_edit():
    path = INSTALL_DIR / "scripts" / "_yaml_edit.py"
    spec = importlib.util.spec_from_file_location("yaml_edit", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load yaml helper: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@lru_cache
def player_config_path() -> Path:
    return INSTALL_DIR / "config" / "player.yaml"


@lru_cache(maxsize=1)
def _yaml_mod():
    return _load_yaml_edit()


# The parsed player.yaml, keyed on the file's identity. Not a per-key cache that
# only our own cfg_set clears: the shell scripts write this file too
# (bt_pair.sh and bt_autoconnect.sh set bluetooth.primary_mac), and a cache
# blind to that kept playing to — and verifying against — the old speaker.
_cfg_lock = threading.Lock()
_cfg_doc: dict = {"sig": None, "data": None}
# One writer at a time in this process: set_value is read-modify-write through
# a fixed player.yaml.tmp, so two threads could lose an update or replace the
# other's temp file from under it.
_cfg_write_lock = threading.Lock()


def _cfg_tree() -> dict:
    path = player_config_path()
    st = path.stat()
    # Taken before the read: a file replaced in between is read new under the
    # old signature, which only costs one extra parse on the next call.
    sig = (st.st_ino, st.st_mtime_ns, st.st_size)
    with _cfg_lock:
        if _cfg_doc["sig"] == sig:
            return _cfg_doc["data"]
    data = _yaml_mod().load(path)
    with _cfg_lock:
        _cfg_doc.update(sig=sig, data=data)
    return data


def cfg_get(key: str):
    return _yaml_mod().get_key(_cfg_tree(), key)


def _cfg_cache_clear() -> None:
    with _cfg_lock:
        _cfg_doc["sig"] = None


# Kept for callers that know a script just wrote the file (set_hostname).
cfg_get.cache_clear = _cfg_cache_clear  # type: ignore[attr-defined]


def cfg_set(key: str, value: str) -> None:
    # set_value edits the one line rather than re-dumping the tree: player.yaml
    # carries the explanation of every option next to it, and this runs at
    # runtime (every pair writes primary_mac), so a dump here would have
    # flattened the file on a live box.
    with _cfg_write_lock:
        _yaml_mod().set_value(player_config_path(), key, value)
        _cfg_cache_clear()


@lru_cache
def get_player() -> PlayerService:
    socket = Path(cfg_get("audio.mpv_socket") or "/run/audio-player/mpv.sock")
    state_dir = Path(cfg_get("paths.state_dir") or "/var/lib/audio-player")
    player = PlayerService(INSTALL_DIR, state_dir, socket, cfg_get, cfg_set)
    player.radio = RadioService(player, INSTALL_DIR, state_dir, cfg_get, cfg_set)
    player.links = get_links()
    player.recorder = Recorder(player, state_dir, cfg_get)
    player.mpv.before_replace = player.recorder.stop_for_new_source
    return player


@lru_cache
def get_links() -> LinksService:
    return LinksService(Path(cfg_get("paths.state_dir") or "/var/lib/audio-player"))


def get_radio() -> RadioService:
    return get_player().radio



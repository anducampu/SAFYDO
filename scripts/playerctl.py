#!/usr/bin/env python3
"""CLI player control — Phase 2."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

INSTALL_DIR = Path("/opt/rpi-audio-player")
sys.path.insert(0, str(INSTALL_DIR))

from backend.services.mpv import MpvError  # noqa: E402
from backend.services.player import PlayerService  # noqa: E402


def cfg_paths() -> tuple:
    import importlib.util

    yaml_edit = INSTALL_DIR / "scripts" / "_yaml_edit.py"
    spec = importlib.util.spec_from_file_location("yaml_edit", yaml_edit)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    config = INSTALL_DIR / "config" / "player.yaml"

    def cfg_get(key: str):
        data = mod.load(config)
        return mod.get_key(data, key)

    def cfg_set(key: str, value: str):
        mod.set_value(config, key, value)

    socket = Path(cfg_get("audio.mpv_socket") or "/run/audio-player/mpv.sock")
    state_dir = Path(cfg_get("paths.state_dir") or "/var/lib/audio-player")
    return INSTALL_DIR, state_dir, socket, cfg_get, cfg_set


def main() -> int:
    parser = argparse.ArgumentParser(description="RPI Audio Player control")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    p_streams = sub.add_parser("streams")
    p_streams.add_argument("--favorites", action="store_true")
    sub.add_parser("usb-scan")
    sub.add_parser("usb-list")
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("toggle")
    sub.add_parser("stop")

    p_play = sub.add_parser("play-stream")
    p_play.add_argument("stream_id")

    p_url = sub.add_parser("play-url")
    p_url.add_argument("url")

    p_file = sub.add_parser("play-file")
    p_file.add_argument("path")

    p_vol = sub.add_parser("volume")
    p_vol.add_argument("level", type=int)

    p_eq = sub.add_parser("eq")
    p_eq.add_argument("state", choices=["on", "off", "status"])

    p_verify = sub.add_parser("verify", help="Verify BT + mpv audio pipeline")
    p_verify.add_argument("--test-tone", action="store_true")
    p_verify.add_argument("--require-playback", action="store_true")
    p_verify.add_argument("--json", action="store_true", help="JSON only, use exit code")

    args = parser.parse_args()
    install_dir, state_dir, socket, cfg_get, cfg_set = cfg_paths()
    player = PlayerService(install_dir, state_dir, socket, cfg_get, cfg_set)

    try:
        if args.cmd == "status":
            print(json.dumps(player.status(), indent=2))
        elif args.cmd == "streams":
            print(json.dumps(player.list_streams(favorites_only=args.favorites), indent=2))
        elif args.cmd == "usb-scan":
            print(json.dumps(player.scan_usb(), indent=2))
        elif args.cmd == "usb-list":
            print(json.dumps(player.list_usb(), indent=2))
        elif args.cmd == "play-stream":
            print(json.dumps(player.play_stream(args.stream_id), indent=2))
        elif args.cmd == "play-url":
            print(json.dumps(player.play_url(args.url), indent=2))
        elif args.cmd == "play-file":
            print(json.dumps(player.play_file(args.path), indent=2))
        elif args.cmd == "pause":
            print(json.dumps(player.pause(True), indent=2))
        elif args.cmd == "resume":
            print(json.dumps(player.pause(False), indent=2))
        elif args.cmd == "toggle":
            print(json.dumps(player.toggle_pause(), indent=2))
        elif args.cmd == "stop":
            print(json.dumps(player.stop(), indent=2))
        elif args.cmd == "volume":
            print(json.dumps(player.set_volume(args.level), indent=2))
        elif args.cmd == "eq":
            if args.state == "on":
                cfg_set("audio.eq_enabled", "true")
            elif args.state == "off":
                cfg_set("audio.eq_enabled", "false")
            print(json.dumps(player.eq.active_preset(), indent=2))
        elif args.cmd == "verify":
            result = player.verify(
                test_tone=args.test_tone,
                require_playback=args.require_playback,
            )
            print(json.dumps(result, indent=2))
            # Exit codes always (the old `if args.json or True` was always true).
            failures = result.get("failures") or []
            if "bluetooth_not_connected" in failures or "primary_mac_empty" in failures:
                return 1
            if "mpv_service_inactive" in failures or "mpv_socket_missing" in failures:
                return 2
            if "audio_device_mismatch" in failures:
                return 3
            if "playback_not_active" in failures:
                return 4
            if "test_tone_no_playback" in failures or "test_tone_mpv_error" in failures:
                return 5
            return 0 if result.get("ok") else 1
    except (MpvError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
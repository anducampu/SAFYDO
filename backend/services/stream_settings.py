"""Online-stream tuning — the mpv counterpart of StreamJockey's "Stream settings".

One definition serves both places the values are used:
  * the backend applies them to the running mpv (set_property), so a change
    needs no restart;
  * scripts/mpv_daemon.sh asks `python3 -m backend.services.stream_settings`
    for the command-line flags, so a restarted mpv starts with them too.

Keys live under `stream:` in player.yaml (plus audio.output_buffer).
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import sys

# key: (default, min, max, kind)
FIELDS: dict[str, tuple] = {
    # 0 = unlimited. ffmpeg's reconnect_max_retries (ffmpeg 7+).
    "retries": (0, 0, 99, int),
    # ffmpeg backs off between reconnects up to this many seconds, then gives up.
    "reconnect_delay_max": (10, 1, 120, int),
    # Forward read-ahead. mpv's default is effectively unlimited (1000 h),
    # bounded only by demuxer-max-bytes: a one-hour link would pull ~50 MB
    # into RAM ahead of the play head. 60 s is plenty against network jitter.
    "cache_secs": (60, 10, 300, int),
    # Seconds of data to refill before playing again after a stall.
    "rebuffer_secs": (1.0, 0.5, 30.0, float),
    # Also wait for rebuffer_secs before the first sound (mpv has one knob
    # for both: cache-pause-wait).
    "prebuffer_on_start": (False, None, None, bool),
    # Connect/read timeout.
    "timeout_secs": (30, 3, 120, int),
    # "" = mpv's default user agent.
    "user_agent": ("", None, 200, str),
    # ALSA/bluealsa output buffer. Below 0.5 s, bluealsa showed underrun bursts.
    "output_buffer": (0.5, 0.2, 2.0, float),
}

CFG_KEY = {k: (f"audio.{k}" if k == "output_buffer" else f"stream.{k}") for k in FIELDS}


def _coerce(key: str, raw):
    default, lo, hi, kind = FIELDS[key]
    if raw is None or raw == "":
        return default
    if kind is bool:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("true", "1", "yes", "on")
    if kind is str:
        text = str(raw).strip()
        if any(not (0x20 <= ord(c) <= 0x7E) for c in text):
            raise ValueError("user agent: printable ASCII only")
        if len(text) > hi:
            raise ValueError(f"user agent: at most {hi} characters")
        return text
    try:
        value = kind(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key}: not a number") from exc
    if not lo <= value <= hi:
        raise ValueError(f"{key}: must be between {lo} and {hi}")
    return value


def read(cfg_get) -> dict:
    out = {}
    for key in FIELDS:
        try:
            out[key] = _coerce(key, cfg_get(CFG_KEY[key]))
        except ValueError:
            out[key] = FIELDS[key][0]  # a hand-edited bad value must not break playback
    return out


def defaults() -> dict:
    return {k: v[0] for k, v in FIELDS.items()}


def validate(patch: dict) -> dict:
    """Coerce + range-check a partial update; raises ValueError on bad input."""
    unknown = set(patch) - set(FIELDS)
    if unknown:
        raise ValueError(f"unknown setting: {sorted(unknown)[0]}")
    return {k: _coerce(k, v) for k, v in patch.items()}


def to_yaml_value(value) -> str:
    """For cfg_set, which quotes on its own when YAML would misread a value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@functools.lru_cache(maxsize=1)
def ffmpeg_has_max_retries() -> bool:
    """reconnect_max_retries arrived in ffmpeg 7 (Trixie ships 7.1, Bookworm
    5.1). An unknown lavf option only earns a warning in mpv, but asking for a
    limit that is silently ignored would be a lie in the UI."""
    exe = shutil.which("ffmpeg")
    if not exe:
        return False
    try:
        out = subprocess.run(
            [exe, "-hide_banner", "-h", "protocol=http"], capture_output=True, text=True, errors="replace", timeout=15
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "reconnect_max_retries" in out


def lavf_options(s: dict) -> str:
    opts = [
        "reconnect=1",
        "reconnect_streamed=1",
        "reconnect_on_network_error=1",
        f"reconnect_delay_max={s['reconnect_delay_max']}",
    ]
    if ffmpeg_has_max_retries():
        opts.append(f"reconnect_max_retries={s['retries'] or -1}")
    return ",".join(opts)


def mpv_properties(s: dict) -> list[tuple[str, object]]:
    """(property, value) pairs for set_property on a running mpv. Cache and
    rebuffer apply at once; timeout, user agent and reconnect apply to the
    next stream opened; output buffer from the next audio reinit."""
    return [
        ("cache-secs", s["cache_secs"]),
        ("cache-pause-wait", s["rebuffer_secs"]),
        ("cache-pause-initial", s["prebuffer_on_start"]),
        ("network-timeout", s["timeout_secs"]),
        ("user-agent", s["user_agent"] or "libmpv"),
        ("demuxer-lavf-o", lavf_options(s)),
        ("audio-buffer", s["output_buffer"]),
    ]


def mpv_flags(s: dict) -> list[str]:
    flags = []
    for prop, value in mpv_properties(s):
        if isinstance(value, bool):
            value = "yes" if value else "no"
        flags.append(f"--{prop}={value}")
    return flags


def _main(argv: list[str]) -> int:
    # `python3 -m backend.services.stream_settings flags` → one flag per line,
    # for mpv_daemon.sh. Falls back to defaults if the config can't be read.
    if argv[1:] != ["flags"]:
        print("usage: python3 -m backend.services.stream_settings flags", file=sys.stderr)
        return 2
    try:
        # The yaml helper directly, not backend.config: that would import the
        # whole player stack and slow every mpv start by a second on a Pi.
        import importlib.util
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location("yaml_edit", root / "scripts" / "_yaml_edit.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        tree = mod.load(root / "config" / "player.yaml")
        settings = read(lambda key: mod.get_key(tree, key))
    except Exception:  # noqa: BLE001 — mpv must start even with a broken config
        settings = defaults()
    print("\n".join(mpv_flags(settings)))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))

#!/usr/bin/env python3
"""Merge template player.yaml with live Pi config — never clobber primary_mac."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("python3-yaml required", file=sys.stderr)
    raise SystemExit(1)

# Entire sections: live values win when set (deploy never clobbers operator config)
# Every section the web UI writes belongs here: one left out is reset to the
# template on every deploy (recording/stream/modules were, 2026-11-18 — the
# recording format kept snapping back to mp3).
PRESERVE_SECTIONS = (
    "bluetooth",
    "network",
    "audio",
    "radio",
    "leds",
    "sources",
    "recording",
    "stream",
    "modules",
    "auth",
    "paths",
    "performance",
)

# Explicit dotted keys always win when non-empty
PRESERVE_IF_SET = (
    "bluetooth.primary_mac",
    "audio.volume",
    "audio.volume_follow_hardware",
    "audio.eq_enabled",
    "audio.resume_on_boot",
)


def load(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def get_key(data: dict, dotted: str):
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def set_key(data: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def deep_merge_defaults(base: dict, defaults: dict) -> dict:
    for key, val in defaults.items():
        if key not in base:
            base[key] = val
        elif isinstance(val, dict) and isinstance(base.get(key), dict):
            deep_merge_defaults(base[key], val)
    return base


def merge_section(live_sec: dict, template_sec: dict) -> dict:
    """Live section values win; template fills missing keys only."""
    out = yaml.safe_load(yaml.safe_dump(template_sec)) or {}
    for key, val in live_sec.items():
        if val is None:
            continue
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = merge_section(val, out[key])
        else:
            out[key] = val
    return out


def merge(template: dict, live: dict) -> dict:
    merged = yaml.safe_load(yaml.safe_dump(template)) or {}
    for section in PRESERVE_SECTIONS:
        live_sec = live.get(section)
        if isinstance(live_sec, dict):
            tmpl_sec = merged.get(section)
            if isinstance(tmpl_sec, dict):
                merged[section] = merge_section(live_sec, tmpl_sec)
            else:
                merged[section] = yaml.safe_load(yaml.safe_dump(live_sec))
    deep_merge_defaults(merged, live)
    # Top-level scalars (hostname, mdns_name) are the board's own: live wins.
    for key, val in live.items():
        if not isinstance(val, dict) and val not in (None, ""):
            merged[key] = val
    for key in PRESERVE_IF_SET:
        live_val = get_key(live, key)
        if live_val is None:
            continue
        if key == "bluetooth.primary_mac" and str(live_val).strip() in ("", "null", "None"):
            continue
        set_key(merged, key, live_val)
    return merged


def _leaves(node, prefix=""):
    """Every scalar leaf as (dotted key, value). None if a list is in the way."""
    for key, value in node.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            yield from _leaves(value, f"{dotted}.")
        elif isinstance(value, (list, tuple)):
            raise TypeError(dotted)
        else:
            yield dotted, value


def render_over_template(template_path: Path, merged: dict) -> str | None:
    """Write the merged values into the template's own text.

    The template is the documentation: every option sits next to the paragraph
    explaining it, and the wedge_reboot block is the only place that switch is
    explained at all. Dumping the merged tree threw all of that away on every
    single deploy, so what shipped to the board was a bare list of keys.

    Returns None when the result cannot be produced faithfully, and the caller
    dumps instead — a correct file without comments beats a pretty wrong one.
    """
    try:
        edit = _load_yaml_edit()
    except Exception:
        return None
    try:
        leaves = list(_leaves(merged))
    except TypeError:
        return None

    text = template_path.read_text()
    template_values = load(template_path)
    for dotted, value in leaves:
        if get_key(template_values, dotted) == value:
            continue
        # YAML spelling, not Python's: the file is written as true/false and a
        # stray "True" in the middle of it reads as a mistake.
        if value is None:
            rendered = ""
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        # Plain first, so the file keeps its unquoted style; quoted second, and
        # the type has to survive. A live config is full of stringly-typed
        # values left by the old writer ('true', '45'), and writing those plain
        # would silently retype them — which the whole-file check below would
        # then reject, sending a perfectly good merge to the dump path.
        placed = False
        for quote in (False, True):
            edited = edit.set_key_in_text(text, dotted, rendered, quote)
            if edited is None:
                return None
            try:
                got = get_key(yaml.safe_load(edited) or {}, dotted)
            except yaml.YAMLError:
                return None
            if type(got) is type(value) and got == value:
                text, placed = edited, True
                break
        if not placed:
            return None

    # One last check on the whole file, not just the key that changed: this is
    # the config the board boots on.
    try:
        if (yaml.safe_load(text) or {}) != merged:
            return None
    except yaml.YAMLError:
        return None
    return text


def _load_yaml_edit():
    import importlib.util

    path = Path(__file__).resolve().parent / "_yaml_edit.py"
    spec = importlib.util.spec_from_file_location("yaml_edit", path)
    if not spec or not spec.loader:
        raise RuntimeError(path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge player.yaml preserving live BT MAC")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--live", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    template = load(args.template)
    live = load(args.live)
    if not template:
        print(f"template missing or empty: {args.template}", file=sys.stderr)
        return 1

    merged = merge(template, live) if live else template
    args.out.parent.mkdir(parents=True, exist_ok=True)

    text = render_over_template(args.template, merged)
    if text is None:
        # Structure changed in a way the line editor cannot express (a new
        # section, a list). Correctness first: dump, and lose the comments.
        text = yaml.safe_dump(merged, default_flow_style=False, sort_keys=False)
    # --out is the live player.yaml the board boots on. Truncate-and-write left
    # it empty or half-written if power went mid-deploy (and the backend could
    # read it half-written meanwhile); the editor's tmp + fsync + rename never
    # does.
    _load_yaml_edit().write_atomic(args.out, text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
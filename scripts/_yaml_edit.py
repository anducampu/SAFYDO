#!/usr/bin/env python3
"""Minimal read/write helper for config/player.yaml.

Writing goes through a surgical line edit rather than a reload-and-dump: this
file is documentation as much as configuration — every option next to the
paragraph explaining it — and yaml.safe_dump strips every comment in the file.
Since cfg_set is called at runtime (primary_mac on every pair, leds, sources),
one dump would have quietly flattened the whole thing on a live box.

The dump path is still there for keys that do not exist yet, and every surgical
edit is verified by re-parsing before it replaces the original.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:
    print("python3-yaml required", file=sys.stderr)
    sys.exit(1)

DEFAULT_CONFIG = Path("/opt/rpi-audio-player/config/player.yaml")

KEY_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z0-9_][A-Za-z0-9_.\-]*):(?P<rest>.*)$")


def load(path: Path) -> dict:
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def save(path: Path, data: dict) -> None:
    """Structural write: the whole tree re-dumped. Loses comments, so it is the
    fallback, not the path any ordinary key change takes."""
    write_atomic(path, yaml.safe_dump(data, default_flow_style=False, sort_keys=False))


def write_atomic(path: Path, text: str) -> None:
    """tmp + fsync + rename, with a temp name of its own.

    A fixed player.yaml.tmp was shared by every writer — the backend and the
    shell scripts (bt_pair.sh, bt_autoconnect.sh) — so one could rename the
    other's half-written file into place, or find its own gone from under it.
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        st = None
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp makes 0600; keep the mode and owner the file had, or give a
        # new file the 0644 a plain open() would have.
        if st is not None:
            os.chmod(tmp, stat.S_IMODE(st.st_mode))
            with contextlib.suppress(PermissionError):
                os.chown(tmp, st.st_uid, st.st_gid)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _write_lock(path: Path):
    """One writer at a time ACROSS processes.

    set_value is read-modify-write. The backend serialises its own threads, but
    the shell scripts write the same file — and they do it at connect time,
    exactly when the backend adopts the speaker's volume — so without a shared
    lock one write silently dropped the other (primary_mac reverting, say).
    The lock file lives in /run/lock (tmpfs): no SD write, and no rsync --delete
    from a deploy can remove it from under a holder. Best effort: a caller that
    cannot open it still writes, just unserialised, as before.
    """
    lock_dir = Path("/run/lock") if os.path.isdir("/run/lock") else path.parent
    lock_path = lock_dir / f"safydo-{path.name}.lock"
    try:
        fh = open(lock_path, "a")
    except OSError:
        yield
        return
    with fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield


def get_key(data: dict, dotted: str):
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def set_key(data: dict, dotted: str, value: str) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def render_scalar(value: str, quote: bool = False) -> str:
    """A YAML scalar for `value`, plain where that is unambiguous.

    `quote` forces the double-quoted form, which is how the caller retries a
    value YAML would otherwise re-read as something else — a bare `yes` is the
    boolean True in YAML 1.1, and storing it plain would change what it means.
    """
    if value == "" or quote:
        return json.dumps(value)
    plain = (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.:/@+\-]*", value)
        and ": " not in value
        and not value.endswith((":", " "))
    )
    # json.dumps produces a double-quoted scalar, which YAML reads the same way.
    return value if plain else json.dumps(value)


def split_comment(rest: str) -> tuple[str, str]:
    """Split the text after `key:` into (value, trailing comment)."""
    quote = ""
    for i, ch in enumerate(rest):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or rest[i - 1] in " \t"):
            return rest[:i], rest[i:]
    return rest, ""


def same_value(parsed, wanted: str) -> bool:
    """Did the file end up meaning what we asked for?

    Type-aware on purpose: YAML reads `wedge_reboot: true` back as the bool
    True, and a naive str() comparison against "true" rejects a perfectly good
    edit — which silently sent every write down the comment-destroying path.
    """
    wanted = wanted.strip()
    if isinstance(parsed, bool):
        return str(parsed).lower() == wanted.lower()
    if parsed is None:
        return wanted.lower() in ("", "~", "null", "none")
    if isinstance(parsed, (int, float)):
        try:
            return float(parsed) == float(wanted)
        except ValueError:
            return False
    return str(parsed) == wanted


def set_key_in_text(text: str, dotted: str, value: str, quote: bool = False) -> str | None:
    """Replace an existing scalar in place, or add it under its parent block."""
    want = dotted.split(".")
    lines = text.splitlines(keepends=True)
    stack: list[tuple[int, str]] = []

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")):
            continue
        m = KEY_RE.match(line.rstrip("\n"))
        if not m:
            continue
        indent = len(m.group("indent").expandtabs(8))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = [k for _, k in stack] + [m.group("key")]
        stack.append((indent, m.group("key")))

        if path != want:
            continue
        rest = m.group("rest")
        current, comment = split_comment(rest)
        if not current.strip():
            return None  # a mapping or an empty block, not a scalar we may edit
        sep = current[: len(current) - len(current.lstrip())] or " "
        pad = current[len(current.rstrip()):] if comment else ""
        newline = "\n" if line.endswith("\n") else ""
        lines[idx] = (
            f"{m.group('indent')}{m.group('key')}:{sep}"
            f"{render_scalar(value, quote)}{pad}{comment}{newline}"
        )
        return "".join(lines)
    return add_key_to_text(lines, want, value, quote)


def add_key_to_text(lines: list[str], want: list[str], value: str, quote: bool = False) -> str | None:
    """Append a key that does not exist yet, under its existing parent block.

    This is the upgrade path — a box whose player.yaml predates a new option —
    and it is the one that most needs the comments kept, since a dump there
    would flatten a file the user has been reading as documentation.
    """
    if len(want) == 1:
        text = "".join(lines)
        pad = "" if text.endswith("\n") else "\n"
        return f"{text}{pad}{want[0]}: {render_scalar(value, quote)}\n"

    parent, leaf = want[:-1], want[-1]
    stack: list[tuple[int, str]] = []
    parent_idx = parent_indent = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")):
            continue
        m = KEY_RE.match(line.rstrip("\n"))
        if not m:
            continue
        indent = len(m.group("indent").expandtabs(8))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = [k for _, k in stack] + [m.group("key")]
        stack.append((indent, m.group("key")))
        if path == parent:
            parent_idx, parent_indent = idx, indent
            break
    if parent_idx is None:
        return None

    # Insert after the parent's last child key, so a trailing comment block
    # (which introduces whatever comes next) stays attached to what it explains.
    child_indent, insert_at = None, None
    for idx in range(parent_idx + 1, len(lines)):
        line = lines[idx]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = KEY_RE.match(line.rstrip("\n"))
        indent = len(m.group("indent").expandtabs(8)) if m else None
        if indent is None or indent <= parent_indent:
            break
        child_indent, insert_at = indent, idx + 1
    if insert_at is None:
        child_indent, insert_at = parent_indent + 2, parent_idx + 1

    lines.insert(insert_at, f"{' ' * child_indent}{leaf}: {render_scalar(value, quote)}\n")
    return "".join(lines)


def set_value(path: Path, key: str, value) -> None:
    """Write one key, keeping the file's comments and layout where possible.

    Never trusts a text edit without re-reading it: a surgical change that
    parses to anything other than what was asked for is discarded. Plain first,
    so the file keeps the unquoted style it is written in; quoted as a retry,
    for values YAML would read back as some other type; the structural dump only
    if both fail.
    """
    value = value if isinstance(value, str) else str(value)
    with _write_lock(path):
        text = path.read_text()
        for quote in (False, True):
            edited = set_key_in_text(text, key, value, quote)
            if edited is None:
                break
            try:
                reparsed = yaml.safe_load(edited) or {}
            except yaml.YAMLError:
                continue
            if same_value(get_key(reparsed, key), value):
                write_atomic(path, edited)
                return

        data = load(path)
        set_key(data, key, value)
        save(path, data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Edit player.yaml keys")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("get")
    g.add_argument("key")

    s = sub.add_parser("set")
    s.add_argument("key")
    s.add_argument("value")

    args = parser.parse_args()
    if not args.config.exists():
        print(f"config not found: {args.config}", file=sys.stderr)
        return 1

    if args.cmd == "get":
        value = get_key(load(args.config), args.key)
        if value is None:
            return 1
        # YAML spelling for booleans: Python's True/False made every shell
        # caller case-fold, and one that forgot (bt_autoconnect.sh comparing
        # against "true") silently read every saved setting as unset.
        if isinstance(value, bool):
            print("true" if value else "false")
        else:
            print(value)
        return 0

    set_value(args.config, args.key, args.value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

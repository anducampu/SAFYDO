"""File sources — browse the USB drive + mounted network drives (SMB/NFS) and
mount/unmount NAS shares from the UI.

The backend runs as root, so mounts are issued directly (no sudo helper).
Network drives are declared in ``network_drives.json`` and mounted read-only
under ``/media/net/<id>``. SMB passwords live in a root-only per-drive
credentials file and are never returned to the UI.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from backend.config import cfg_get
from backend.textsafe import display

AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".ogg", ".wav", ".m4a", ".aac", ".opus", ".wma",
    ".aiff", ".aif", ".alac", ".ape", ".m4b", ".mpc", ".dsf", ".wv",
}

STATE_DIR = Path("/var/lib/audio-player")
DRIVES_FILE = STATE_DIR / "network_drives.json"
CREDS_DIR = STATE_DIR / "smb-creds"
NET_BASE = Path("/media/net")

# Conservative: a share/host must look like a hostname/IP + a path segment, so
# nothing weird reaches the mount argv (we already use list-form subprocess).
# Not starting with "-": an NFS source is "<host>:/<share>", and "-ofoo:/x"
# would reach mount as an option.
_HOST_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]{0,254}$")
_SHARE_RE = re.compile(r"^[A-Za-z0-9 ._$/-]{1,255}$")
_SAFE_LINE = re.compile(r"[\r\n\x00]")


def _usb_mount() -> Path:
    try:
        return Path(cfg_get("paths.usb_mount") or "/media/usb")
    except Exception:
        return Path("/media/usb")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return s or "drive"


def _is_mounted(path: Path) -> bool:
    try:
        proc = subprocess.run(
            ["findmnt", "-rn", "--target", str(path)],
            capture_output=True, text=True, errors="replace", timeout=5, check=False,
        )
        # findmnt returns the mount whose target is at or above path; only count
        # it when the mountpoint IS this dir (not the parent filesystem).
        for line in proc.stdout.splitlines():
            if line.split(" ", 1)[0] == str(path):
                return True
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def _load_drives() -> list[dict]:
    if not DRIVES_FILE.is_file():
        return []
    try:
        data = json.loads(DRIVES_FILE.read_text())
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_drives(drives: list[dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DRIVES_FILE.write_text(json.dumps(drives, indent=2) + "\n")


def _find_drive(drive_id: str) -> dict | None:
    return next((d for d in _load_drives() if d["id"] == drive_id), None)


def list_sources() -> list[dict]:
    """USB + every configured network drive, with live mount state."""
    usb = _usb_mount()
    sources = [{
        "id": "usb", "label": "USB drive", "type": "usb",
        "path": str(usb), "mounted": _is_mounted(usb) or _has_files(usb),
    }]
    for d in _load_drives():
        mp = NET_BASE / d["id"]
        sources.append({
            "id": d["id"], "label": d["label"], "type": d["type"],
            "host": d.get("host"), "share": d.get("share"),
            "path": str(mp), "mounted": _is_mounted(mp),
        })
    return sources


def _has_files(path: Path) -> bool:
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:
        return False


def _source_root(source_id: str) -> Path:
    if source_id == "usb":
        return _usb_mount()
    if _find_drive(source_id):
        return NET_BASE / source_id
    raise ValueError(f"unknown source: {source_id}")


def browse(source_id: str, subpath: str = "") -> dict:
    """List folders + audio files in a directory under a source (path-safe)."""
    root = _source_root(source_id).resolve()
    subpath = (subpath or "").strip("/")
    target = (root / subpath).resolve() if subpath else root
    if target != root and not target.is_relative_to(root):
        raise ValueError("path escapes the source root")
    if not target.is_dir():
        raise ValueError("folder not found — the drive may not be mounted")

    dirs: list[dict] = []
    files: list[dict] = []
    try:
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        raise ValueError(f"cannot read folder: {exc}") from exc
    for p in entries:
        if p.name.startswith("."):
            continue
        try:
            rel = str(p.relative_to(root))
            if p.is_dir():
                dirs.append({"name": display(p.name), "rel": rel})
            elif p.suffix.lower() in AUDIO_EXTENSIONS:
                files.append({"name": display(p.name), "rel": rel, "path": str(p)})
        except OSError:
            continue

    parent = str(Path(subpath).parent) if subpath else None
    if parent == ".":
        parent = ""
    return {
        "source": source_id,
        "path": subpath,
        "parent": parent,
        "dirs": dirs,
        "files": files,
    }


def _write_smb_credentials(drive_id: str, username: str, password: str, domain: str) -> Path:
    CREDS_DIR.mkdir(parents=True, exist_ok=True)
    CREDS_DIR.chmod(0o700)
    creds = CREDS_DIR / drive_id
    lines = [f"username={username}", f"password={password}"]
    if domain:
        lines.append(f"domain={domain}")
    creds.write_text("\n".join(lines) + "\n")
    creds.chmod(0o600)
    return creds


def _mount_config(d: dict) -> tuple[str, str, list[str]]:
    """Return (fstype, source_spec, options) for a drive config."""
    mp = NET_BASE / d["id"]
    if d["type"] == "smb":
        src = f"//{d['host']}/{d['share']}"
        opts = ["ro", "iocharset=utf8", "vers=3.0", "uid=0", "gid=0", "file_mode=0644", "dir_mode=0755"]
        creds = CREDS_DIR / d["id"]
        if creds.is_file():
            opts.append(f"credentials={creds}")
        else:
            opts.append("guest")
        if d.get("options"):
            opts.append(d["options"])
        return "cifs", src, opts
    # nfs
    src = f"{d['host']}:/{d['share'].lstrip('/')}"
    opts = ["ro", "soft", "timeo=50", "retrans=2"]
    if d.get("options"):
        opts.append(d["options"])
    return "nfs", src, opts


def mount_drive(drive_id: str) -> dict:
    d = _find_drive(drive_id)
    if not d:
        raise ValueError(f"unknown drive: {drive_id}")
    mp = NET_BASE / drive_id
    mp.mkdir(parents=True, exist_ok=True)
    if _is_mounted(mp):
        return {"ok": True, "mounted": True, "already": True}
    fstype, src, opts = _mount_config(d)
    proc = subprocess.run(
        ["mount", "-t", fstype, src, str(mp), "-o", ",".join(opts)],
        capture_output=True, text=True, errors="replace", timeout=30, check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or "mount failed"
        return {"ok": False, "mounted": False, "detail": detail}
    return {"ok": True, "mounted": True}


def add_network_drive(payload: dict) -> dict:
    dtype = str(payload.get("type", "")).strip().lower()
    host = str(payload.get("host", "")).strip()
    share = str(payload.get("share", "")).strip().strip("/")
    label = str(payload.get("label", "")).strip() or share or host
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    domain = str(payload.get("domain", "")).strip()
    options = str(payload.get("options", "")).strip()

    if dtype not in ("smb", "nfs"):
        raise ValueError("type must be 'smb' or 'nfs'")
    if not _HOST_RE.match(host):
        raise ValueError("invalid host / server address")
    if not share or not _SHARE_RE.match(share):
        raise ValueError("invalid share / export path")
    # The password too: it lands on its own line of the credentials file, where
    # a newline would add lines of its own.
    for field in (username, password, domain, options):
        if _SAFE_LINE.search(field):
            raise ValueError("invalid characters in credentials/options")

    drives = _load_drives()
    base_id = _slug(label)
    drive_id = base_id
    existing = {d["id"] for d in drives}
    if drive_id in existing:
        drive_id = f"{base_id}-{uuid.uuid4().hex[:4]}"

    record = {
        "id": drive_id, "label": label, "type": dtype,
        "host": host, "share": share,
        "username": username, "domain": domain, "options": options,
    }
    if dtype == "smb" and username:
        _write_smb_credentials(drive_id, username, password, domain)

    drives.append(record)
    _save_drives(drives)

    result = mount_drive(drive_id)
    if not result.get("ok"):
        # Roll back: a drive that won't mount shouldn't linger in config.
        _forget(drive_id)
        raise ValueError(result.get("detail") or "could not mount network drive")
    return {"ok": True, "id": drive_id, "label": label, "mounted": True}


def _forget(drive_id: str) -> None:
    _save_drives([d for d in _load_drives() if d["id"] != drive_id])
    creds = CREDS_DIR / drive_id
    if creds.exists():
        creds.unlink(missing_ok=True)


def remove_network_drive(drive_id: str) -> dict:
    if not _find_drive(drive_id):
        raise ValueError(f"unknown drive: {drive_id}")
    mp = NET_BASE / drive_id
    if _is_mounted(mp):
        proc = subprocess.run(["umount", str(mp)], capture_output=True, text=True, errors="replace", timeout=20, check=False)
        if proc.returncode != 0:
            subprocess.run(["umount", "-l", str(mp)], capture_output=True, text=True, errors="replace", timeout=10, check=False)
    _forget(drive_id)
    try:
        if mp.is_dir() and not _is_mounted(mp):
            mp.rmdir()
    except OSError:
        pass
    return {"ok": True, "removed": drive_id}


def remount_all() -> None:
    """Mount every configured drive that isn't mounted — called at startup."""
    for d in _load_drives():
        mp = NET_BASE / d["id"]
        if not _is_mounted(mp):
            try:
                mount_drive(d["id"])
            except (ValueError, OSError, subprocess.SubprocessError):
                continue

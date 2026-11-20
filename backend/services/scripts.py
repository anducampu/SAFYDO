"""Run one of scripts/*.sh from the backend.

The one place that does it, so every caller gets the same timeout handling:
a script that hangs comes back as an ordinary failure (exit 124, timeout(1)'s
convention) with whatever it printed so far plus a line saying it gave up.
Escaping as TimeoutExpired made the endpoint answer 500 with a traceback in
the journal, and the UI showed nothing useful.
"""

from __future__ import annotations

import subprocess

from backend.config import INSTALL_DIR

TIMEOUT_RC = 124


def _text(value) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")


def run_script(name: str, *args: str, timeout: int = 120, input: str | None = None) -> subprocess.CompletedProcess:
    script = INSTALL_DIR / "scripts" / name
    if not script.is_file():
        return subprocess.CompletedProcess([str(script), *args], 127, "", f"[{name}] ERROR: script not found: {script}")
    try:
        return subprocess.run(
            [str(script), *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            input=input,
        )
    except subprocess.TimeoutExpired as exc:
        said = _text(exc.stderr).strip()
        return subprocess.CompletedProcess(
            exc.cmd,
            TIMEOUT_RC,
            _text(exc.stdout),
            (said + "\n" if said else "") + f"[{name}] ERROR: gave up after {timeout}s (the script did not finish)",
        )
    except OSError as exc:
        return subprocess.CompletedProcess([str(script), *args], 126, "", f"[{name}] ERROR: could not run: {exc}")


def last_error(proc: subprocess.CompletedProcess, fallback: str) -> str:
    """The script's last line of output, without the "[script] ERROR:" prefix."""
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return (lines[-1] if lines else fallback).split("ERROR:", 1)[-1].strip() or fallback

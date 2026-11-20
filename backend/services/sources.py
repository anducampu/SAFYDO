"""Config-aware wrapper around the source plugins.

Reads each plugin's enabled state + config from player.yaml (sources.<id>.*),
and only ever calls a plugin that the user enabled. Plugins are network-bound
and best-effort — a failing one returns an empty list, never raises here.
"""

from __future__ import annotations

from backend.config import cfg_get
from backend.sources import PROVIDERS


def _enabled(pid: str) -> bool:
    val = cfg_get(f"sources.{pid}.enabled")
    return str(val).lower() in ("true", "1", "yes")


def _provider_cfg(pid: str) -> dict:
    if pid == "jamendo":
        return {"client_id": cfg_get("sources.jamendo.client_id") or ""}
    return {}


def _provider(pid: str):
    cls = PROVIDERS.get(pid)
    return cls(_provider_cfg(pid)) if cls else None


def list_sources() -> list[dict]:
    """All registered plugins with their enabled/configured state (for the UI)."""
    out: list[dict] = []
    for pid, cls in PROVIDERS.items():
        prov = _provider(pid)
        out.append(
            {
                "id": pid,
                "label": cls.label,
                "enabled": _enabled(pid),
                "needs_config": bool(getattr(cls, "needs_config", False)),
                "configured": bool(prov.available()) if prov else False,
            }
        )
    return out


def genres(pid: str) -> list[str]:
    if not _enabled(pid):
        return []
    prov = _provider(pid)
    try:
        return prov.genres() if prov and prov.available() else []
    except Exception:
        return []


def browse(pid: str, genre: str | None = None, query: str | None = None, page: int = 0) -> list[dict]:
    if not _enabled(pid):
        return []
    prov = _provider(pid)
    if not prov or not prov.available():
        return []
    try:
        return prov.browse(genre=genre, query=query, page=page)
    except Exception:
        return []

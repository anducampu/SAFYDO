"""5-band stereo EQ via mpv audio filters."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.services.mpv import MpvClient
    from backend.services.spectrum import SpectrumTap

EQ_BANDS_HZ = (60, 250, 1000, 4000, 12000)
EQ_WIDTHS = (80, 200, 500, 2000, 5000)


class EqService:
    def __init__(
        self,
        presets_path: Path,
        player_config_get,
        cfg_set,
        mpv: MpvClient | None = None,
    ) -> None:
        self.presets_path = presets_path
        self._cfg_get = player_config_get
        self._cfg_set = cfg_set
        self._mpv = mpv
        self._applied = False
        # Source-specific filters ahead of EQ. See backend.services.radio.
        # Per instance: a class-level [] is one list shared by every instance.
        self.pre_filters: list[str] = []

    def bind_mpv(self, mpv: MpvClient) -> None:
        self._mpv = mpv

    def _load(self) -> dict:
        return json.loads(self.presets_path.read_text())

    def _save(self, data: dict) -> None:
        # Atomic like streams.json: a power cut mid-write left a truncated file,
        # and active_preset() — read by every status frame — then raised on it.
        tmp = self.presets_path.with_suffix(".json.part")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        try:  # keep owner/mode, so it stays editable over SSH
            st = self.presets_path.stat()
            os.chmod(tmp, st.st_mode & 0o7777)
            os.chown(tmp, st.st_uid, st.st_gid)
        except OSError:
            pass
        tmp.replace(self.presets_path)

    def enabled(self) -> bool:
        return str(self._cfg_get("audio.eq_enabled") or "false").lower() == "true"

    def set_enabled(self, enabled: bool) -> None:
        self._cfg_set("audio.eq_enabled", "true" if enabled else "false")

    def set_preset(self, preset_id: str) -> None:
        data = self._load()
        presets = data.get("presets") or {}
        if preset_id not in presets:
            raise ValueError(f"unknown EQ preset: {preset_id}")
        data["active_preset"] = preset_id
        self._save(data)

    def set_custom_bands(self, bands: list[float]) -> None:
        """Save manual band gains into the persistent "custom" preset and
        make it active — survives restarts and preset switches."""
        if len(bands) != len(EQ_BANDS_HZ):
            raise ValueError(f"expected {len(EQ_BANDS_HZ)} band gains, got {len(bands)}")
        clamped = [max(-12.0, min(12.0, round(float(g), 1))) for g in bands]
        data = self._load()
        presets = data.setdefault("presets", {})
        custom = presets.setdefault("custom", {"name": "Custom"})
        custom["bands"] = clamped
        data["active_preset"] = "custom"
        self._save(data)

    def list_presets(self) -> list[dict]:
        data = self._load()
        active = data.get("active_preset", "flat")
        out: list[dict] = []
        for key, preset in (data.get("presets") or {}).items():
            out.append({"id": key, "name": preset.get("name", key), "active": key == active})
        return out

    def active_preset(self) -> dict:
        data = self._load()
        key = data.get("active_preset", "flat")
        presets = data.get("presets") or {}
        preset = presets.get(key) or presets.get("flat") or {}
        bands = list(preset.get("bands") or [0, 0, 0, 0, 0])[:5]
        while len(bands) < 5:
            bands.append(0.0)
        return {
            "id": key,
            "name": preset.get("name", key),
            "bands": bands,
            "band_freqs": list(EQ_BANDS_HZ),
            "enabled": self.enabled(),
            "eq_applied": self._applied,
        }

    def build_filter_chain(self, *, viz_active: bool, viz: SpectrumTap | None = None) -> list[str]:
        filters: list[str] = list(self.pre_filters)
        if self.enabled():
            bands = self.active_preset().get("bands") or [0, 0, 0, 0, 0]
            for freq, width, gain in zip(EQ_BANDS_HZ, EQ_WIDTHS, bands[:5]):
                if gain:
                    filters.append(f"equalizer=f={freq}:width_type=h:width={width}:g={gain}")
        if viz_active and viz is not None:
            filters.extend(viz.filter_specs())
        return filters

    def apply(self, *, viz_active: bool = False, viz: SpectrumTap | None = None) -> bool:
        self._applied = False
        if not self._mpv or not self._mpv.available():
            if viz is not None:
                viz.set_attached(False)
            return False
        filters = self.build_filter_chain(viz_active=viz_active, viz=viz)
        if viz_active and viz is not None and any("astats" in f for f in filters):
            viz.prepare_log()
        try:
            self._mpv.command("af", "set", ",".join(filters))
        except Exception:
            try:
                self._mpv.command("af", "set", "")
            except Exception:
                pass
            if viz is not None:
                viz.set_attached(False)
            return False
        if viz is not None:
            viz.set_attached(viz_active and bool(filters))
        self._applied = self.enabled() and any(
            g for g in (self.active_preset().get("bands") or [])[:5]
        )
        return True
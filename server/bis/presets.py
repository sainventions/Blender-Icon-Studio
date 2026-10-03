"""shared/presets.json access (+ swatch URLs for pre-rendered material swatches)."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .config import Settings

ALL_APPEARANCES = ["light", "dark", "clear-light", "clear-dark", "tinted-light", "tinted-dark"]
_FALLBACK_TIERS = {"draft": 512, "preview": 512, "final": 1024, "ultra": 2048}


class PresetStore:
    """Reads presets.json, re-reading it when the file changes (designers can tweak it live)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._data: dict[str, Any] = {}

    def raw(self) -> dict[str, Any]:
        path = self.settings.presets_path
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return self._data or {"version": 1, "materials": {}, "lighting": {}, "platforms": {},
                                  "appearances": {}, "quality": {}, "colorModes": {}, "looks": {}}
        with self._lock:
            if mtime != self._mtime:
                self._data = json.loads(path.read_text(encoding="utf-8"))
                self._mtime = mtime
            return self._data

    def with_swatches(self) -> dict[str, Any]:
        data = json.loads(json.dumps(self.raw()))  # deep copy
        data.pop("$comment", None)
        data.setdefault("looks", {})
        sw_dir: Path = self.settings.swatches_dir
        for key, mat in (data.get("materials") or {}).items():
            png = sw_dir / f"{key}.png"
            if png.is_file():
                mat["swatch"] = f"/swatches/{key}.png?v={int(png.stat().st_mtime)}"
        return data

    # -- helpers used by the job runners
    def tier_size(self, quality: str) -> int:
        q = (self.raw().get("quality") or {}).get(quality) or {}
        return int(q.get("size") or _FALLBACK_TIERS.get(quality, 512))

    def tier_engine(self, quality: str) -> str:
        q = (self.raw().get("quality") or {}).get(quality) or {}
        return str(q.get("engine") or ("eevee" if quality == "draft" else "cycles"))

    def platform_appearances(self, platform: str) -> list[str]:
        p = (self.raw().get("platforms") or {}).get(platform) or {}
        apps = p.get("appearances")
        return [a for a in apps if a in ALL_APPEARANCES] if apps else list(ALL_APPEARANCES)

    def material_keys(self) -> list[str]:
        return list((self.raw().get("materials") or {}).keys())

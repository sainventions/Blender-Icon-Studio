"""Access to ``shared/presets.json`` (single source of truth for materials, lighting, quality tiers)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

_ROOT: Optional[Path] = None
_CACHE: Optional[dict] = None


def set_root(root: os.PathLike | str | None) -> None:
    """Repository root (contains ``shared/presets.json``). Defaults to the parent of this package."""
    global _ROOT, _CACHE
    _ROOT = Path(root).resolve() if root else None
    _CACHE = None


def repo_root() -> Path:
    return _ROOT or Path(__file__).resolve().parents[1]


def load() -> dict:
    global _CACHE
    if _CACHE is None:
        with open(repo_root() / "shared" / "presets.json", encoding="utf-8") as fh:
            _CACHE = json.load(fh)
    return _CACHE


def material_ids() -> list[str]:
    return list(load()["materials"].keys())


def material(preset: str) -> dict:
    mats = load()["materials"]
    return mats.get(preset) or mats["liquid_glass"]


def material_params(preset: str, overrides: Optional[dict] = None) -> dict[str, Any]:
    """Preset param defaults overlaid with user overrides (unknown keys kept, values clamped to range)."""
    spec = material(preset).get("params", {})
    out: dict[str, Any] = {k: v.get("default") for k, v in spec.items()}
    for k, v in (overrides or {}).items():
        if v is None:
            continue
        p = spec.get(k)
        if p and p.get("type") == "number":
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            lo, hi = p.get("min"), p.get("max")
            if lo is not None:
                v = max(float(lo), v)
            if hi is not None:
                v = min(float(hi), v)
        elif p and p.get("type") == "enum" and v not in p.get("options", []):
            continue
        out[k] = v
    return out


def paint_mode(preset: str) -> str:
    return material(preset).get("paint", "base")


def lighting(preset: str) -> dict:
    lights = load()["lighting"]
    return lights.get(preset) or lights["studio"]


def quality(tier: str) -> dict:
    q = load()["quality"]
    return q.get(tier) or q["draft"]


def color_mode(mode: str) -> dict:
    cm = load()["colorModes"]
    return cm.get(mode) or cm["neutral"]

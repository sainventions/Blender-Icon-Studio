"""Test doubles for the server: a fake ``bis.svg`` module (the real one is built concurrently by workstream A)
and helpers to create isolated apps. The FakeBridge lives in ``bis.blender.fake``.

FakeSvg understands ``<rect>``, ``<circle>``, ``<ellipse>`` and (bbox-less) ``<path>`` elements with a plain
``fill`` attribute: the first element becomes the detected plate, the others the foreground elements.
It writes the same files as the real pipeline (source.svg, normalized.svg, elements.json, cache/…).
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import (
    Canvas,
    Element,
    FillSolid,
    GeometryBundle,
    Layer,
    LayerDepth,
    LayerGeometry,
    Plate,
    Project,
    Region,
    SourceInfo,
    Spline,
    SplinePoint,
)

TEST_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100">
  <rect id="plate" x="2" y="2" width="96" height="96" rx="20" fill="#2563eb"/>
  <circle id="ring" cx="50" cy="50" r="30" fill="#ffffff"/>
  <rect id="bar" x="30" y="45" width="40" height="10" fill="#f59e0b"/>
  <circle id="dot" cx="50" cy="50" r="6" fill="#ef4444"/>
</svg>
"""

_SHAPE_RE = re.compile(r"<(rect|circle|ellipse|path)\b([^>]*)/?>", re.I)
_ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')


@dataclass
class FakeImportResult:
    source: SourceInfo
    elements: list[Element]
    layers: list[Layer]
    canvas: Canvas
    warnings: list[str] = field(default_factory=list)


def _f(v: str | None, d: float = 0.0) -> float:
    try:
        return float(v) if v is not None else d
    except ValueError:
        return d


class FakeSvg:
    """Duck-typed stand-in for the ``bis.svg`` module."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    # -------------------------------------------------------------------------- parsing
    @staticmethod
    def _viewbox(svg: str) -> tuple[float, float, float, float]:
        m = re.search(r'viewBox\s*=\s*"([^"]+)"', svg)
        if m:
            parts = [float(p) for p in re.split(r"[\s,]+", m.group(1).strip())[:4]]
            if len(parts) == 4:
                return tuple(parts)  # type: ignore[return-value]
        return 0.0, 0.0, 100.0, 100.0

    def _shapes(self, svg: str) -> list[tuple[str, dict[str, str]]]:
        return [(m.group(1).lower(), dict(_ATTR_RE.findall(m.group(2)))) for m in _SHAPE_RE.finditer(svg)]

    @staticmethod
    def _bbox_svg(tag: str, a: dict[str, str], i: int) -> tuple[float, float, float, float]:
        if tag == "rect":
            x, y = _f(a.get("x")), _f(a.get("y"))
            return x, y, x + _f(a.get("width")), y + _f(a.get("height"))
        if tag in ("circle", "ellipse"):
            cx, cy = _f(a.get("cx")), _f(a.get("cy"))
            rx = _f(a.get("r") or a.get("rx"), 1)
            ry = _f(a.get("r") or a.get("ry"), 1)
            return cx - rx, cy - ry, cx + rx, cy + ry
        return 10.0 + i, 10.0 + i, 60.0 + i, 60.0 + i

    # -------------------------------------------------------------------------- API
    def import_svg(self, svg_bytes: bytes, filename: str, project_dir: Path, strategy: str = "smart"):
        self.calls.append("import_svg")
        text = svg_bytes.decode("utf-8", "replace")
        if "<svg" not in text:
            raise ValueError("not an SVG")
        vb = self._viewbox(text)
        x0, y0, w, h = vb
        k = 2.0 / max(w, h)
        cx, cy = x0 + w / 2, y0 + h / 2

        def art(px: float, py: float) -> tuple[float, float]:
            return (px - cx) * k, -(py - cy) * k

        elements: list[Element] = []
        for i, (tag, a) in enumerate(self._shapes(text)):
            bx0, by0, bx1, by1 = self._bbox_svg(tag, a, i)
            (ax0, ay1), (ax1, ay0) = art(bx0, by0), art(bx1, by1)
            fill = a.get("fill") or "#808080"
            if not fill.startswith("#"):
                fill = "#808080"
            elements.append(Element(
                id=f"e{i}", name=a.get("id") or f"{tag} {i}", paint=FillSolid(color=fill),
                bbox=(ax0, ay0, ax1, ay1), area=max((ax1 - ax0) * (ay1 - ay0) / 4.0, 0.0),
            ))
        plate_detected = len(elements) > 1 and elements[0].area > 0.5
        canvas = Canvas()
        fg = elements
        if plate_detected:
            plate_el = elements[0]
            canvas.plate = Plate(fill=FillSolid(color=plate_el.paint.color))  # type: ignore[union-attr]
            canvas.art.scale = 2.0 / (plate_el.bbox[2] - plate_el.bbox[0])
            fg = elements[1:]
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "source.svg").write_bytes(svg_bytes)
        (project_dir / "normalized.svg").write_bytes(svg_bytes)
        (project_dir / "elements.json").write_text(json.dumps([e.model_dump(mode="json") for e in elements]))
        source = SourceInfo(filename=filename, viewBox=vb, plateDetected=plate_detected)
        return FakeImportResult(source, elements, self._split([e.id for e in fg], strategy), canvas)

    @staticmethod
    def _split(ids: list[str], strategy: str) -> list[Layer]:
        if strategy == "single":
            groups = [ids] if ids else []
        elif strategy == "element":
            groups = [[i] for i in ids]
        else:
            groups = [ids[i:i + 2] for i in range(0, len(ids), 2)]
        return [Layer(id=f"l{n}", name=f"Layer {n + 1}", elementIds=g, depth=LayerDepth(z=round(n * 0.13, 4)))
                for n, g in enumerate(groups)]

    def split_layers(self, project_dir: Path, project: Project, strategy: str) -> list[Layer]:
        self.calls.append("split_layers")
        assigned = [e for l in project.layers for e in l.elementIds]
        plate_ids = {project.elements[0].id} if project.source.plateDetected and project.elements else set()
        ids = [e.id for e in project.elements if e.id not in plate_ids or e.id in assigned]
        return self._split(ids, strategy)

    def merge_layers(self, project_dir: Path, project: Project, layer_ids: list[str]) -> list[Layer]:
        self.calls.append("merge_layers")
        order = {e.id: i for i, e in enumerate(project.elements)}
        layers = [l.model_copy(deep=True) for l in project.layers]
        sel = [l for l in layers if l.id in layer_ids]
        if len(sel) < 2:
            raise ValueError("need two layers")
        target = sel[0]
        target.elementIds = sorted({e for l in sel for e in l.elementIds}, key=lambda e: order[e])
        return [l for l in layers if l is target or l.id not in layer_ids]

    def split_layer(self, project_dir: Path, project: Project, layer_id: str, mode: str = "elements") -> list[Layer]:
        self.calls.append("split_layer")
        out: list[Layer] = []
        for l in project.layers:
            if l.id != layer_id or len(l.elementIds) < 2:
                out.append(l)
                continue
            for j, e in enumerate(l.elementIds):
                n = l.model_copy(deep=True)
                n.id, n.name, n.elementIds = f"{l.id}-{j}", f"{l.name} {j + 1}", [e]
                out.append(n)
        return out

    def _hash(self, project: Project) -> str:
        key = json.dumps([[l.id, l.elementIds, l.mode] for l in project.layers]) + project.id
        return hashlib.sha1(key.encode()).hexdigest()[:12]

    def build_geometry(self, project_dir: Path, project: Project, url_prefix: str) -> GeometryBundle:
        self.calls.append("build_geometry")
        from PIL import Image

        h = self._hash(project)
        cache = project_dir / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        els = {e.id: e for e in project.elements}
        vb = project.source.viewBox
        layers: dict[str, LayerGeometry] = {}
        for layer in project.layers:
            regions = []
            boxes = []
            for eid in layer.elementIds:
                e = els[eid]
                boxes.append(e.bbox)
                regions.append(Region(elementId=eid, paint=e.paint, splines=[self._rect_spline(e.bbox, e.name)]))
            if not boxes:
                continue
            ub = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
            tex = cache / f"{layer.id}-{h}.png"
            if not tex.is_file():
                Image.new("RGBA", (16, 16), (255, 255, 255, 255)).save(tex)
            svg = cache / f"{layer.id}-{h}.svg"
            if not svg.is_file():
                rects = "".join(
                    f'<rect x="{vb[0] + (els[e].bbox[0] + 1) / 2 * vb[2]:.2f}" y="{vb[1] + (1 - els[e].bbox[3]) / 2 * vb[3]:.2f}" '
                    f'width="{(els[e].bbox[2] - els[e].bbox[0]) / 2 * vb[2]:.2f}" height="{(els[e].bbox[3] - els[e].bbox[1]) / 2 * vb[3]:.2f}" '
                    f'fill="{getattr(els[e].paint, "color", "#888888")}"/>'
                    for e in layer.elementIds
                )
                svg.write_text(
                    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb[0]} {vb[1]} {vb[2]} {vb[3]}" '
                    f'width="{vb[2]}" height="{vb[3]}">{rects}</svg>'
                )
            layers[layer.id] = LayerGeometry(
                layerId=layer.id, hash=h, silhouette=[self._rect_spline(ub)], regions=regions,
                safeRadius=0.05, bbox=ub,
                maxRadius=round(min(ub[2] - ub[0], ub[3] - ub[1]) / 2 if layer.mode == "combined"
                                else max(min(b[2] - b[0], b[3] - b[1]) / 2 for b in boxes), 5),
                texture=f"{url_prefix}/cache/{tex.name}", texturePath=str(tex),
                svg=f"{url_prefix}/cache/{svg.name}",
            )
        bundle = GeometryBundle(projectId=project.id, hash=h, viewBox=vb, layers=layers)
        (cache / f"geometry-{h}.json").write_text(json.dumps(bundle.model_dump(mode="json")))
        return bundle

    @staticmethod
    def _rect_spline(b, name: str = "") -> Spline:
        x0, y0, x1, y1 = b
        if "ring" in name or "dot" in name:  # circles as 4-point beziers
            cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
            c = 0.5523
            pts = [
                SplinePoint(co=(cx + rx, cy), hl=(cx + rx, cy - c * ry), hr=(cx + rx, cy + c * ry)),
                SplinePoint(co=(cx, cy + ry), hl=(cx + c * rx, cy + ry), hr=(cx - c * rx, cy + ry)),
                SplinePoint(co=(cx - rx, cy), hl=(cx - rx, cy + c * ry), hr=(cx - rx, cy - c * ry)),
                SplinePoint(co=(cx, cy - ry), hl=(cx - c * rx, cy - ry), hr=(cx + c * rx, cy - ry)),
            ]
            return Spline(points=pts)
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        return Spline(points=[SplinePoint(co=c, hl=c, hr=c) for c in corners])

    def geometry_path(self, project_dir: Path, bundle: GeometryBundle) -> Path:
        return project_dir / "cache" / f"geometry-{bundle.hash}.json"

    def thumbnail_png(self, svg_bytes: bytes, size: int = 256) -> bytes:
        self.calls.append("thumbnail_png")
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGBA", (size, size), (37, 99, 235, 255)).save(buf, "PNG")
        return buf.getvalue()

    def layer_thumbnail_png(self, project_dir: Path, project: Project, layer_id: str, size: int = 96) -> bytes:
        self.calls.append("layer_thumbnail_png")
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGBA", (size, size), (255, 255, 255, 200)).save(buf, "PNG")
        return buf.getvalue()


def make_test_settings(tmp_path: Path, **overrides: Any):
    """Settings for an isolated app: temp workspace, no worker autostart, fast auto-preview."""
    from .config import Settings

    kw: dict[str, Any] = dict(
        workspace=tmp_path / "workspace",
        blender_exe=None,
        start_worker=False,
        auto_preview_delay=0.05,
        gpu_poll_interval=60.0,
        swatches_override=tmp_path / "swatches",
    )
    kw.update(overrides)
    return Settings.from_env(**kw)

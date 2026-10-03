"""Acceptance over the full corpus: all 68 icons in ``samle icons/`` + the 12 research test icons
import without exceptions, geometry bundles validate, plates are detected on >= 61 corpus icons and
the recomposed layer SVGs (plate + layers) match the normalised source within 1 %."""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from conftest import CORPUS_DIR, SVGTESTS_DIR

import bis.svg as svg
from bis.models import GeometryBundle, Project
from bis.svg import raster

CORPUS = sorted(CORPUS_DIR.glob("*.svg"))
ALL = CORPUS + sorted(SVGTESTS_DIR.glob("*.svg"))
RESULTS: dict = {}

pytestmark = pytest.mark.corpus


def _recompose(pdir: Path, bundle: GeometryBundle, project: Project, w: int, h: int) -> np.ndarray:
    imgs = []
    if bundle.plate:
        imgs.append(raster.render_svg(Path(bundle.plate["svgPath"]).read_text(encoding="utf-8"), w, h))
    for L in project.layers:
        imgs.append(raster.render_svg((pdir / "cache" / Path(bundle.layers[L.id].svg).name)
                                      .read_text(encoding="utf-8"), w, h))
    return raster.composite(imgs) if imgs else np.zeros((h, w, 4), np.uint8)


def test_corpus_size():
    assert len(CORPUS) == 68 and len(ALL) == 80


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.stem)
def test_icon(path, import_icon):
    t0 = time.perf_counter()
    res, project, pdir = import_icon(path)
    t_import = time.perf_counter() - t0
    t0 = time.perf_counter()
    bundle = svg.build_geometry(pdir, project, f"/files/projects/{project.id}", texture_size=128)
    t_geom = time.perf_counter() - t0
    GeometryBundle.model_validate_json(svg.geometry_path(pdir, bundle).read_text(encoding="utf-8"))
    assert set(bundle.layers) == {L.id for L in project.layers}
    for L in project.layers:
        lg = bundle.layers[L.id]
        assert lg.silhouette and lg.regions and lg.safeRadius >= 0
        assert Path(lg.texturePath).exists()
        assert 0 <= L.depth.bevel <= 0.045 + 1e-9
    vb = res.source.viewBox
    w = 160
    h = max(1, int(round(w * vb[3] / vb[2])))
    normalized = raster.render_svg((pdir / "normalized.svg").read_text(encoding="utf-8"), w, h)
    recomp = raster.diff_pct(normalized, _recompose(pdir, bundle, project, w, h))
    is_raster = any(e.kind == "image" for e in res.elements)
    if not is_raster:
        assert recomp <= 1.0
    RESULTS[path.stem] = {
        "corpus": path.parent == CORPUS_DIR, "layers": len(res.layers), "plate": res.source.plateDetected,
        "shape": res.canvas.shape, "warnings": len(res.warnings), "ms": round((t_import + t_geom) * 1000),
        "recomp": recomp, "raster": is_raster,
    }


def test_corpus_summary():
    corpus = {k: v for k, v in RESULTS.items() if v["corpus"]}
    if len(corpus) < len(CORPUS):
        pytest.skip("run together with test_icon")
    plates = sum(1 for v in corpus.values() if v["plate"])
    assert plates >= 61, plates
    lines = [f"{'icon':24s} {'layers':>6s} {'plate':>9s} {'warn':>4s} {'ms':>6s} {'recomp%':>8s}"]
    for k, v in sorted(RESULTS.items()):
        lines.append(f"{k[:24]:24s} {v['layers']:6d} {(v['shape'] if v['plate'] else '-'):>9s} "
                     f"{v['warnings']:4d} {v['ms']:6d} {v['recomp']:8.3f}{' raster' if v['raster'] else ''}")
    lines.append(f"plates: {plates}/{len(corpus)} corpus icons")
    print("\n" + "\n".join(lines))

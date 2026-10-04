"""Export the 68-icon corpus as the stacking fixture of the web parity test (web/src/features/editor/stacking.test.mjs).

For every icon: import with the server's SVG pipeline (round 8), build the geometry bundle, and record what the web sees
(layers + the bundle's splines / images / maxRadius) together with the server's bis.stacking results computed from
the BUNDLE (shapes_from_bundle - the same input the web has): heights H, overlap lists, the import z, re-stacks of
several edited variants, stack_gap recognition and interpenetrations.

Run from the repository root with the server venv (imports go to a temporary directory):
    .venv/Scripts/python.exe web/src/features/editor/fixtures/export_stack_fixture.py
writes web/src/features/editor/fixtures/stack-corpus.json.gz (gzip, mtime 0). Optional args: <out.json(.gz)> [icon,icon,...]
"""
from __future__ import annotations

import gzip
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO / "server"))
sys.path.insert(0, str(REPO / "tests"))
HERE = Path(__file__).resolve().parent

import bis.svg as svg  # noqa: E402
from bis import stacking  # noqa: E402
from conftest import make_project  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "stack-corpus.json.gz"
ONLY = set(sys.argv[2].split(",")) if len(sys.argv) > 2 else None
R = 6


def r6(v):
    return round(float(v), R)


def spl(s):
    pts = s.points if hasattr(s, "points") else s["points"]
    out = []
    for p in pts:
        co, hl, hr = (p.co, p.hl, p.hr) if hasattr(p, "co") else (p["co"], p["hl"], p["hr"])
        out.append([r6(co[0]), r6(co[1]), r6(hl[0]), r6(hl[1]), r6(hr[0]), r6(hr[1])])
    hole = s.hole if hasattr(s, "hole") else s.get("hole", False)
    return {"h": bool(hole), "p": out}


def layer_json(L):
    return {"id": L.id, "mode": L.mode, "visible": L.visible, "locked": False,
            "transform": {"x": r6(L.transform.x), "y": r6(L.transform.y), "scale": r6(L.transform.scale)},
            "depth": {"z": L.depth.z, "thickness": L.depth.thickness, "bevel": L.depth.bevel,
                      "bevelSegments": L.depth.bevelSegments, "inflate": L.depth.inflate}}


def zs(layers):
    return [float(L.depth.z) for L in layers]


def restacked(layers, shapes, art, **kw):
    c = [L.model_copy(deep=True) for L in layers]
    return stacking.restack(c, shapes, art_scale=art.scale, art_offset=art, **kw)


def record(name, project, pdir, pid, rules):
    bundle = svg.build_geometry(pdir, project, f"/files/projects/{pid}", texture_size=16)
    art = project.canvas.art
    layers = project.layers
    if not layers:
        return None
    shapes = stacking.shapes_from_bundle(bundle, layers, art.scale)
    geo = {}
    for L in layers:
        lg = bundle.layers[L.id]
        geo[L.id] = {"hash": lg.hash, "maxRadius": lg.maxRadius,
                     "silhouette": [spl(s) for s in lg.silhouette],
                     "regions": [{"elementId": r.elementId, "opacity": r.opacity, "zSub": r.zSub,
                                  "splines": [spl(s) for s in r.splines]} for r in lg.regions],
                     "images": [{"elementId": im["elementId"], "bbox": [r6(v) for v in im["bbox"]]} for im in (dict(x) if not isinstance(x, dict) else x for x in lg.images)]}
    hs = stacking.heights(layers, shapes, art.scale)
    lower = stacking.overlap_lists(layers, shapes, art_scale=art.scale, art_offset=art)
    # pairwise footprint distances (canvas units) - to flag borderline pairs near the clearance
    fps = stacking.footprints(layers, shapes, art.scale, art)
    dist = {}
    for i in range(len(fps)):
        for j in range(i):
            a, b = fps[i], fps[j]
            if a is None or b is None or a.is_empty or b.is_empty:
                continue
            dist[f"{j},{i}"] = r6(a.distance(b))
    inl = [stacking.in_layer_height(L, shapes[L.id], stacking.layer_scale(L, art.scale)) for L in layers]
    cases = {"import": zs(layers), "restack": restacked(layers, shapes, art)}
    # edited variants, all re-stacked by the overlap-aware rule
    v = [L.model_copy(deep=True) for L in layers]
    v[0].depth.thickness = 0.3
    v[0].depth.bevel = 0.15
    variants = {"thick0": v}
    v = [L.model_copy(deep=True) for L in layers]
    for L in v:
        L.depth.inflate = 1.0
    variants["inflate1"] = v
    v = [L.model_copy(deep=True) for L in layers]
    v[0].transform.x += 0.35
    v[0].transform.scale = 0.8
    variants["move0"] = v
    v = [L.model_copy(deep=True) for L in layers]
    for L in v:
        L.mode = "combined"
    variants["combined"] = v
    var_out = {}
    for key, vl in variants.items():
        sh = stacking.shapes_from_bundle(bundle, vl, art.scale) if key != "move0" else stacking.shapes_from_bundle(bundle, vl, art.scale)
        var_out[key] = {"layers": [layer_json(L) for L in vl],
                        "H": stacking.heights(vl, sh, art.scale),
                        "z": restacked(vl, sh, art)}
    var_out["gap05"] = {"z": restacked(layers, shapes, art, gap=0.05)}
    # stack recognition (server stack_gap) of some stacks
    rec = {"import": stacking.stack_gap(layers, shapes, art_scale=art.scale, art_offset=art)}
    seq = [L.model_copy(deep=True) for L in layers]
    z = rules["stackLift"]
    for L, h in zip(seq, hs):
        L.depth.z = round(z, 5)
        z += h + rules["stackGap"]
    rec["sequential"] = {"z": zs(seq), "gap": stacking.stack_gap(seq, shapes, art_scale=art.scale, art_offset=art)}
    leg = [L.model_copy(deep=True) for L in layers]
    for i, L in enumerate(leg):
        L.depth.z = round(i * 0.13, 5)
    rec["legacy"] = {"z": zs(leg), "gap": stacking.stack_gap(leg, shapes, art_scale=art.scale, art_offset=art)}
    hand = [L.model_copy(deep=True) for L in layers]
    if hand:
        hand[-1].depth.z = round(hand[-1].depth.z + 0.1, 5)
    rec["hand"] = {"z": zs(hand), "gap": stacking.stack_gap(hand, shapes, art_scale=art.scale, art_offset=art)}
    flat = [L.model_copy(deep=True) for L in layers]
    for L in flat:
        L.depth.z = 0.0
    rec["flat"] = {"z": zs(flat), "gap": stacking.stack_gap(flat, shapes, art_scale=art.scale, art_offset=art),
                   "interpenetrations": [[j, i, r6(d)] for j, i, d in
                                         stacking.interpenetrations(flat, shapes, art_scale=art.scale,
                                                                    art_offset=art)]}
    # a hand-placed stack whose bottom layer then got thicker (QA N5): server lift_overlaps result + collisions
    n5 = [L.model_copy(deep=True) for L in hand]
    if n5:
        n5[0].depth.thickness = 0.4
        n5[0].depth.bevel = 0.2
    sh5 = stacking.shapes_from_bundle(bundle, n5, art.scale)
    rec["n5"] = {"layers": [layer_json(L) for L in n5],
                 "interpenetrations": [[j, i, r6(d)] for j, i, d in
                                       stacking.interpenetrations(n5, sh5, art_scale=art.scale, art_offset=art)]}
    kinds = {e.id: e.kind for e in project.elements}
    return {
        "art": {"scale": art.scale, "x": art.x, "y": art.y},
        "layers": [dict(layer_json(L), image=stacking.is_image_layer(L, kinds)) for L in layers],
        "geometry": geo,
        "H": hs, "inLayer": inl, "lower": lower, "dist": dist,
        "z": cases, "variants": var_out, "recognise": rec,
    }


def main():
    corpus = sorted((REPO / "samle icons").glob("*.svg"))
    work = Path(tempfile.mkdtemp(prefix="bis-stack-fixture-"))
    rules = stacking.geometry_rules()
    out = {"rules": rules, "note": "server bis.stacking from shapes_from_bundle (round 8)", "icons": {}}
    for n, path in enumerate(corpus):
        name = path.stem
        if ONLY and name not in ONLY:
            continue
        t0 = time.perf_counter()
        pdir = work / f"p{n}"
        res = svg.import_svg(path.read_bytes(), path.name, pdir, "smart")
        project = make_project(res, project_id=f"p{n}", name=name)
        entry = record(name, project, pdir, f"p{n}", rules)
        if entry is None:
            print(f"{name}: no layers", flush=True)
            continue
        out["icons"][name] = entry
        # merge the bottom two layers (the server's own structural edit + re-stack): in-layer stacking of pieces
        if len(project.layers) >= 2:
            try:
                merged = svg.merge_layers(pdir, project, [project.layers[0].id, project.layers[1].id])
            except Exception as e:  # noqa: BLE001 - z-order: skip
                print("  merge skipped:", e)
            else:
                mp = project.model_copy(deep=True)
                mp.layers = merged
                me = record(name + "+merge", mp, pdir, f"p{n}m", rules)
                if me is not None:
                    out["icons"][name + "+merge"] = me
        if len(project.layers) >= 3:
            try:
                merged = svg.merge_layers(pdir, project, [L.id for L in project.layers])
            except Exception as e:  # noqa: BLE001 - z-order: skip
                print("  merge-all skipped:", e)
            else:
                mp = project.model_copy(deep=True)
                mp.layers = merged
                me = record(name + "+all", mp, pdir, f"p{n}a", rules)
                if me is not None:
                    out["icons"][name + "+all"] = me
        print(f"{name}: {len(project.layers)} layers, {time.perf_counter() - t0:.2f}s", flush=True)
    shutil.rmtree(work, ignore_errors=True)
    data = json.dumps(out, separators=(",", ":")).encode("utf-8")
    if OUT.suffix == ".gz":
        with open(OUT, "wb") as f, gzip.GzipFile(fileobj=f, mode="wb", compresslevel=9, mtime=0, filename="") as g:
            g.write(data)
    else:
        OUT.write_bytes(data)
    print("wrote", OUT, OUT.stat().st_size)


if __name__ == "__main__":
    main()

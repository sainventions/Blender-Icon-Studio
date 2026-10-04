"""Height-field bodies on REAL corpus geometry, checked inside Blender 5.0 (PLAN §11 Geometry acceptance):

* every piece (layer regions, combined silhouettes) at the default depth, at maximum roundness (bevel =
  thickness / 2) and with inflate 1 is watertight (zero non-manifold edges), never self-intersects (BVH overlap
  of non-adjacent faces), has no custom normal facing away from its face and a positive volume;
* the build is fast: cold (outline → CDT → mesh with custom normals) and cached (LRU hit) times per icon.

Default run: the icons the user named (Gemini star tips, Ti84 keys, Canvas dots, Gmail wedge, Drive, Settings gear
holes, Spotify bars, Contacts, Home, iMessage) plus the worker fixtures. ``BIS_FULL_CORPUS=1`` checks all 68 icons.

This file is also the Blender-side script: ``blender -b --factory-startup --python <this file> -- <index.json>
<out.json> [names...]`` (bpy present -> run the checks and write the JSON report).
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

try:
    import bpy  # noqa: F401  (inside Blender: this file is the checker script)
except ImportError:
    bpy = None

SETTINGS = (("default", None, 0.0), ("maxround", "max", 0.0), ("inflate1", None, 1.0))
NAMED = ["Gemini", "Ti84", "Canvas", "Gmail", "Drive", "Settings", "Spotify", "Contacts", "Home", "iMessage",
         "Maps", "Photos", "Discord", "Calculator", "Find Device", "Vanced Neon", "Syno Photos"]


def _pieces(proj: dict, bundle: dict):
    """(layer id, splines, thickness, bevel, segments, scale) of every body the scene builds (local units)."""
    sa = float(proj["canvas"]["art"].get("scale", 1.0))
    for L in proj["layers"]:
        g = bundle["layers"].get(L["id"])
        if not g:
            continue
        dp = L["depth"]
        S = sa * float((L.get("transform") or {}).get("scale", 1.0))
        th = float(dp.get("thickness", 0.1)) / S
        bev = min(float(dp.get("bevel", 0.045)), float(dp.get("thickness", 0.1)) / 2) / S
        pieces = [g["silhouette"]] if L.get("mode") == "combined" else [r["splines"] for r in g["regions"]]
        for k, spl in enumerate(pieces):
            yield f"{L['id']}/{k}", spl, th, bev, int(dp.get("bevelSegments", 6)), S


def _blender_main(argv: list) -> None:
    sys.path.insert(0, str(REPO))
    from blender_worker import heightfield as H
    index = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    out = Path(argv[1])
    names = argv[2:] or sorted(index)
    report = {}
    for name in names:
        e = index[name]
        proj = json.loads(Path(e["project"]).read_text(encoding="utf-8"))
        bundle = json.loads(Path(e["geometryPath"]).read_text(encoding="utf-8"))
        res = {}
        for tag, bmode, infl in SETTINGS:
            totals = {"pieces": 0, "nonManifold": 0, "selfIntersections": 0, "invertedNormals": 0,
                      "misoriented": 0, "nonPositiveVolume": 0, "failed": []}
            pieces = list(_pieces(proj, bundle))
            t0 = time.perf_counter()
            meshes = []
            for pid, spl, th, bev, seg, S in pieces:
                b = th / 2 if bmode == "max" else bev
                me, _info = H.piece_mesh({"icon": name, "p": pid, "tag": tag}, spl, th, b, infl, seg, S)
                meshes.append((pid, me))
            cold = (time.perf_counter() - t0) * 1000.0
            t0 = time.perf_counter()
            for pid, spl, th, bev, seg, S in pieces:
                b = th / 2 if bmode == "max" else bev
                H.piece_mesh({"icon": name, "p": pid, "tag": tag}, spl, th, b, infl, seg, S)
            cached = (time.perf_counter() - t0) * 1000.0
            for pid, me in meshes:
                if me is None:
                    continue
                totals["pieces"] += 1
                ck = H.check_mesh(me)
                bad = False
                for k in ("nonManifold", "selfIntersections", "invertedNormals"):
                    totals[k] += ck[k]
                    bad |= ck[k] > 0
                if ck["volume"] <= 0:
                    totals["nonPositiveVolume"] += 1
                    bad = True
                if bad:
                    totals["failed"].append({"piece": pid, **ck})
            res[tag] = {**totals, "coldMs": round(cold, 1), "cachedMs": round(cached, 3)}
        report[name] = res
        print(name, {t: (r["pieces"], r["coldMs"], r["nonManifold"], r["selfIntersections"], r["invertedNormals"])
                     for t, r in res.items()}, flush=True)
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")


if bpy is not None:
    if __name__ == "__main__":
        _blender_main(sys.argv[sys.argv.index("--") + 1:])
else:
    import pytest

    sys.path.insert(0, str(HERE))
    import worker_client as wc  # noqa: E402

    pytestmark = pytest.mark.skipif(not wc.blender_available(), reason="Blender 5.0 not installed")

    @pytest.fixture(scope="module")
    def corpus_index() -> tuple[Path, list]:
        import make_fixtures
        out = HERE / "_fixtures" / "corpus"          # its own index: never races the worker suites' fixtures
        index_path = out / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
        full = os.environ.get("BIS_FULL_CORPUS") == "1"
        names = sorted(p.stem for p in make_fixtures.CORPUS.glob("*.svg")) if full else NAMED
        missing = [n for n in names if n not in index or not Path(index[n]["geometryPath"]).exists()]
        if missing:
            index = make_fixtures.make(missing, out)
        return index_path, [n for n in names if n in index]

    def test_bodies_are_watertight_and_never_self_intersect(corpus_index, tmp_path):
        index_path, names = corpus_index
        out = tmp_path / "heightfield_report.json"
        p = subprocess.run([str(wc.BLENDER), "-b", "--factory-startup", "--python", str(Path(__file__).resolve()),
                            "--", str(index_path), str(out), *names],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
        assert out.is_file(), p.stdout[-3000:] + p.stderr[-3000:]
        report = json.loads(out.read_text(encoding="utf-8"))
        assert sorted(report) == sorted(names)
        failures = [(n, t, r["failed"][:2]) for n, v in report.items() for t, r in v.items() if r["failed"]]
        assert not failures, failures
        total = sum(r["pieces"] for v in report.values() for r in v.values())
        assert total >= 3 * len(names)
        # timing table (pytest -s): cold build (outline -> CDT -> mesh) and cached, per icon and setting
        print(f"\n{'icon':18s} " + " ".join(f"{t:>18s}" for t, _, _ in SETTINGS))
        for n in names:
            print(f"{n:18s} " + " ".join(f"{report[n][t]['coldMs']:9.1f}/{report[n][t]['cachedMs']:6.2f} ms"
                                         for t, _, _ in SETTINGS))
        cold = [report[n]["default"]["coldMs"] for n in names]
        cached = [r["cachedMs"] for v in report.values() for r in v.values()]
        # generous bounds: other GPU / CPU users share the machine (measured: median ~50 ms, max < 200 ms)
        assert statistics.median(cold) < 150, cold
        assert max(cold) < 800, cold
        assert max(cached) < 20, cached

    _APEX_EXPR = """
import sys, json
sys.path.insert(0, {repo!r})
from blender_worker import geometry as G, heightfield as H
disc = [G.circle_spline(0.0, 0.0, 0.3)]
out = {{}}
for tag, t, b, k, seg in (("dome3", 0.1, 0.05, 1.0, 3), ("dome6", 0.1, 0.05, 1.0, 6), ("half6", 0.1, 0.05, 0.5, 6),
                          ("sphere6", 0.6, 0.3, 0.0, 6), ("flat6", 0.1, 0.05, 0.0, 6)):
    arr = H.build(disc, t, b, k, seg, 1.0)
    out[tag] = dict(half=arr["info"]["half"], D=arr["info"]["D"], **H.check_arrays(arr))
slab = H.build([G.rect_spline(-0.3, -0.2, 0.3, 0.2)], 0.1, 0.0, 0.0, 6, 1.0)
quads = int(((list(slab["starts"][1:]) + [len(slab["loops"])]) - slab["starts"] == 4).sum())
out["slab"] = dict(H.check_arrays(slab), sharp=len(slab["sharp"]), outline=quads)
open({out!r}, "w").write(json.dumps(out))
"""

    def test_domes_and_bevelled_spheres_reach_their_apex(tmp_path):
        """A disc of radius D: inflate k gives half height e + b + k·D, bevel = radius gives a sphere of radius D —
        at the APEX, not at a flat cap where the rings stop (3.4 % low at 6 segments, 13 % at 3)."""
        out = tmp_path / "apex.json"
        expr = _APEX_EXPR.format(repo=str(REPO), out=str(out))
        p = subprocess.run([str(wc.BLENDER), "-b", "--factory-startup", "--python-expr", expr],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        assert out.is_file(), p.stdout[-3000:] + p.stderr[-3000:]
        r = json.loads(out.read_text(encoding="utf-8"))
        D = r["dome6"]["D"]
        assert D == pytest.approx(0.3, rel=0.005)
        for tag in ("dome3", "dome6"):
            assert r[tag]["half"] == pytest.approx(0.05 + D, rel=2e-3), tag
        assert r["half6"]["half"] == pytest.approx(0.05 + 0.5 * D, rel=2e-3)
        assert r["sphere6"]["half"] == pytest.approx(0.3, rel=2e-3)
        assert r["flat6"]["half"] == pytest.approx(0.05, rel=1e-9)
        for v in r.values():
            assert v["nonManifold"] == 0 and v["misoriented"] == 0 and v["invertedNormals"] == 0 and v["volume"] > 0
        # a flat slab (no bevel, no inflate) is all creases: every corner normal is its face's own (the wall corners
        # were shaded with the corner bisector — a sharp slab looked rounded); rim loops + 4 vertical corner edges
        slab = r["slab"]
        assert slab["minNormalDot"] > 0.999
        assert slab["sharp"] == 2 * slab["outline"] + 4

    _POISSON_EXPR = """
import sys, json, math, time
import numpy as np
sys.path.insert(0, {repo!r})
from blender_worker import geometry as G, heightfield as H

def top(arr):
    V, L, S = arr["verts"], arr["loops"], arr["starts"]
    sizes = np.diff(np.append(S, len(L)))
    tri = np.nonzero(sizes == 3)[0]
    T = L[S[tri[:len(tri) // 2]][:, None] + np.arange(3)]
    return V, T

def crease(arr):
    # largest angle between adjacent top faces in the upper dome (all corners above 45 % of the half height)
    V, T = top(arr)
    P = V[T]
    fn = np.cross(P[:, 1] - P[:, 0], P[:, 2] - P[:, 0])
    fn /= np.linalg.norm(fn, axis=1)[:, None]
    e = np.concatenate([T[:, [0, 1]], T[:, [1, 2]], T[:, [2, 0]]])
    f = np.tile(np.arange(len(T)), 3)
    k = np.minimum(e[:, 0], e[:, 1]) * len(V) + np.maximum(e[:, 0], e[:, 1])
    o = np.argsort(k, kind="stable")
    k, f = k[o], f[o]
    same = k[1:] == k[:-1]
    fa, fb = f[:-1][same], f[1:][same]
    zt = P[:, :, 2].min(1)
    m = (zt[fa] > 0.45 * V[:, 2].max()) & (zt[fb] > 0.45 * V[:, 2].max())
    return float(np.degrees(np.arccos(np.clip((fn[fa] * fn[fb]).sum(1), -1, 1)))[m].max())

out = {{}}
# ellipse x²/a² + y²/b² <= 1: u = c(1 − x²/a² − y²/b²) -> dome = k·D·sqrt(u / c)
a, b, k = 0.5, 0.12, 1.0
pts = [(a * math.cos(2 * math.pi * i / 96), b * math.sin(2 * math.pi * i / 96)) for i in range(96)]
arr = H.build([G.poly_spline(pts)], 0.1, 0.03, k, 6, 1.0)
V, T = top(arr)
inner = np.unique(T)
P = V[inner]
rings = H.piece_rings([G.poly_spline(pts)])
A_, B_, _ = H._segments(rings)
d = H.nearest(P[:, :2], A_, B_)[0]
u = np.clip(1 - (P[:, 0] / a) ** 2 - (P[:, 1] / b) ** 2, 0, 1)
dome = P[:, 2] - H.profile(d, 0.1, 0.03, 0.0, 1.0)
D = arr["info"]["D"]
out["ellipse"] = dict(meanErr=float(np.abs(dome - k * D * np.sqrt(u)).mean()), D=D, half=arr["info"]["half"],
                      **H.check_arrays(arr))
# a long stadium of half width w (b = 0, wall e = t/2): u = 2(w² − y²) mid-length -> dome = k·w·sqrt(1 − y²/w²), a round tube
w = 0.04
st = [(x, -w) for x in np.linspace(-0.4, 0.4, 9)] + [(0.4 + w * math.sin(t), -w * math.cos(t)) for t in np.linspace(0.3, math.pi - 0.3, 6)] \\
     + [(x, w) for x in np.linspace(0.4, -0.4, 9)] + [(-0.4 - w * math.sin(t), w * math.cos(t)) for t in np.linspace(0.3, math.pi - 0.3, 6)]
arr = H.build([G.poly_spline(st)], 0.02, 0.0, 1.0, 6, 1.0)
V, T = top(arr)
mid = V[(np.abs(V[:, 0]) < 0.15) & (V[:, 2] > 0)]
rel = (mid[:, 2] - 0.01) / max(float(mid[:, 2].max()) - 0.01, 1e-9)       # minus the wall half height e = t/2
shape = np.sqrt(np.clip(1 - (mid[:, 1] / w) ** 2, 0, 1))
out["tube"] = dict(err=float(np.abs((rel - shape) * (np.abs(mid[:, 1]) < 0.9 * w)).max()),
                   ridge=float(mid[:, 2].max()), **H.check_arrays(arr))
# corpus shapes at inflate 1: no fins / creases on the dome, fast
for name, spl, t, bev in json.loads(open({shapes!r}).read()):
    t0 = time.perf_counter()
    arr = H.build(spl, t, bev, 1.0, 6, 1.0)
    ms = (time.perf_counter() - t0) * 1000
    out[name] = dict(crease=crease(arr), ms=ms, iterations=arr["info"]["poisson"]["iterations"], **H.check_arrays(arr))
open({out!r}, "w").write(json.dumps(out))
"""

    def test_poisson_dome_is_round_and_creaseless(corpus_index, tmp_path):
        """PLAN §11 round 7: the inflate dome is k·D·sqrt(u/u_max) with −∇²u = 4 (u = 0 on the outline). An ellipse
        matches the analytic solution, a thin stadium becomes a ROUND tube (semicircular cross-section), and the
        corpus shapes that grew creased fins with the distance dome (Gemini's tips, iMessage's tail, Gmail's legs)
        stay smooth: adjacent top faces of the upper dome meet at < 30° (the distance dome: 66–92°)."""
        index_path, _names = corpus_index
        index = json.loads(index_path.read_text(encoding="utf-8"))
        shapes = []
        for name in ("Gemini", "iMessage", "Gmail"):
            proj = json.loads(Path(index[name]["project"]).read_text(encoding="utf-8"))
            bundle = json.loads(Path(index[name]["geometryPath"]).read_text(encoding="utf-8"))
            L = proj["layers"][0]
            g = bundle["layers"][L["id"]]
            spl = g["silhouette"] if L.get("mode") == "combined" else g["regions"][0]["splines"]
            sa = float(proj["canvas"]["art"].get("scale", 1.0))
            shapes.append([name, spl, L["depth"]["thickness"] / sa, min(L["depth"]["bevel"], L["depth"]["thickness"] / 2) / sa])
        sp = tmp_path / "shapes.json"
        sp.write_text(json.dumps(shapes), encoding="utf-8")
        out = tmp_path / "poisson.json"
        expr = _POISSON_EXPR.format(repo=str(REPO), out=str(out), shapes=str(sp))
        p = subprocess.run([str(wc.BLENDER), "-b", "--factory-startup", "--python-expr", expr],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        assert out.is_file(), p.stdout[-3000:] + p.stderr[-3000:]
        r = json.loads(out.read_text(encoding="utf-8"))
        for v in r.values():
            assert v["nonManifold"] == 0 and v["misoriented"] == 0 and v["invertedNormals"] == 0 and v["volume"] > 0
        e = r["ellipse"]
        assert e["half"] == pytest.approx(0.05 + e["D"], rel=1e-6)       # q = 1 at the apex: H = t + 2·k·D
        assert e["meanErr"] < 0.01 * e["D"], e
        assert r["tube"]["err"] < 0.08, r["tube"]                         # semicircular cross-section
        for name in ("Gemini", "iMessage", "Gmail"):
            assert r[name]["crease"] < 30.0 if name != "Gmail" else r[name]["crease"] < 45.0, (name, r[name])
            assert r[name]["ms"] < 200 and r[name]["iterations"] < 400, (name, r[name])

    _SCENE_EXPR = """
import sys, json
sys.path.insert(0, {repo!r})
import bpy
from mathutils.bvhtree import BVHTree
from blender_worker import presets as P, appearance as AP
from blender_worker.defaults import norm_bundle, norm_project
from blender_worker.scene import SceneBuilder
P.set_root({repo!r})
cases = json.loads(open({cases!r}).read())
sb = SceneBuilder()
sb.reset()

def overlaps(prefix):
    obs = sorted((o for o in bpy.data.objects if o.name.startswith(prefix) and o.type == "MESH"), key=lambda o: o.name)
    trees = [BVHTree.FromPolygons([o.matrix_world @ v.co for v in o.data.vertices],
                                  [list(p.vertices) for p in o.data.polygons]) for o in obs]
    return sum(len(trees[i].overlap(trees[j])) for i in range(len(obs)) for j in range(i + 1, len(obs)))

out = {{}}
for name, (proj, bundle) in cases.items():
    info = sb.build(proj, bundle, "light")
    bpy.context.view_layer.update()                  # world matrices (a render evaluates the depsgraph itself)
    lid = proj["layers"][0]["id"]
    obs = sorted((o for o in bpy.data.objects if o.name.startswith(f"BIS {{lid}} r")), key=lambda o: o.name)
    eff = AP.resolve(norm_project(proj), "light", norm_bundle(bundle))["project"]
    hulls = sb.subject_hulls(eff, norm_bundle(bundle))
    out[name] = dict(stats=info["stats"], overlap=overlaps(f"BIS {{lid}} r"),
                     lo=[min((o.matrix_world @ v.co).z for v in o.data.vertices) for o in obs],
                     hi=[max((o.matrix_world @ v.co).z for v in o.data.vertices) for o in obs],
                     span=[h[2] - h[1] for h in hulls if h[3] == lid][0])
open({out!r}, "w").write(json.dumps(out))
"""

    def test_pieces_of_one_layer_never_interpenetrate(corpus_index, tmp_path):
        """QA r8 #6: pieces of one layer that TOUCH (Secure Folder's translucent tab and opaque folder share an edge;
        Calendar's page and the digits in its holes) pull back from each other; a piece that OVERLAPS an earlier one (a translucent disc over a square) is stacked on
        it by their real heights. Framing uses the real layer height: thickness + 2·inflate·maxRadius."""
        import test_worker_quality as T
        index_path, _names = corpus_index
        index = json.loads(index_path.read_text(encoding="utf-8"))
        sf = index["Secure Folder"]
        cases = {"sf": [json.loads(Path(sf["project"]).read_text(encoding="utf-8")),
                        json.loads(Path(sf["geometryPath"]).read_text(encoding="utf-8"))]}
        lay = T.layer("A", z=0.0)
        g = T.geo([("sq", [T.square(0.4)], "#3366ff", 1.0), ("disc", [T.circle(0.25, 0.2, 0.1)], "#ffffff", 0.6)])
        cases["stack"] = list(T.scene([lay], {"A": g}))
        lay = T.layer("A", z=0.0)
        lay["depth"]["inflate"] = 0.5
        g = T.geo([("d", [T.circle(0.3)], "#3366ff", 1.0)])
        g["maxRadius"] = 0.4                                   # the contract value wins over the worker's estimate
        cases["maxr"] = list(T.scene([lay], {"A": g}))
        # round 8: H = max(rule height, in-layer stacked height) — here the rule (maxRadius 1.0) is the taller one
        lay = T.layer("A", z=0.0)
        lay["depth"]["inflate"] = 0.5
        g = T.geo([("sq", [T.square(0.4)], "#3366ff", 1.0), ("disc", [T.circle(0.25, 0.2, 0.1)], "#ffffff", 0.6)])
        g["maxRadius"] = 1.0
        cases["both"] = list(T.scene([lay], {"A": g}))
        # round 9: Calendar's L1 + L2 merged — the page with the digits cut out as holes and the digits filling them
        # (outlines flattened differently, they touch): pulling back only the digits' VERTICES left the page's corners
        # poking into them between two samples (79 intersecting face pairs, the full body height deep)
        cal = json.loads((HERE / "data" / "calendar_merged_layer.json").read_text(encoding="utf-8"))
        cases["calendar"] = [cal["project"], cal["geometry"]]
        cp = tmp_path / "cases.json"
        cp.write_text(json.dumps(cases), encoding="utf-8")
        out = tmp_path / "scene.json"
        expr = _SCENE_EXPR.format(repo=str(REPO), cases=str(cp), out=str(out))
        p = subprocess.run([str(wc.BLENDER), "-b", "--factory-startup", "--python-expr", expr],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        assert out.is_file(), p.stdout[-3000:] + p.stderr[-3000:]
        r = json.loads(out.read_text(encoding="utf-8"))
        assert r["sf"]["stats"].get("insetPieces") == 1 and r["sf"]["overlap"] == 0, r["sf"]
        assert not r["sf"]["stats"].get("stackedPieces")       # touching only: nothing changes height
        st = r["stack"]
        assert st["stats"].get("stackedPieces") == 1 and st["overlap"] == 0, st
        assert st["lo"][1] >= st["hi"][0] + 0.001, st          # the disc rests on the square
        assert st["span"] == pytest.approx(st["hi"][1] - st["lo"][0], abs=0.01), st
        assert r["maxr"]["span"] == pytest.approx(0.1 + 2 * 0.5 * 0.4, rel=1e-6), r["maxr"]
        both = r["both"]
        assert both["stats"].get("stackedPieces") == 1 and both["overlap"] == 0, both
        assert both["hi"][1] - both["lo"][0] < 1.1                    # the stacked bodies are lower than the rule ...
        assert both["span"] == pytest.approx(0.1 + 2 * 0.5 * 1.0, rel=1e-6), both   # ... so the rule frames them
        cal = r["calendar"]
        assert cal["stats"].get("insetPieces", 0) >= 1 and not cal["stats"].get("stackedPieces"), cal["stats"]
        assert cal["overlap"] == 0, cal

    _TUBE_EXPR = """
import sys, json
import numpy as np
sys.path.insert(0, {repo!r})
from blender_worker import geometry as G, heightfield as H

def folds(arr, xmax=None):
    V, L, S = arr["verts"], arr["loops"], arr["starts"]
    sizes = np.diff(np.append(S, len(L)))
    tri = np.nonzero(sizes == 3)[0]
    T = L[S[tri[:len(tri) // 2]][:, None] + np.arange(3)]
    P = V[T]
    fn = np.cross(P[:, 1] - P[:, 0], P[:, 2] - P[:, 0])
    fn /= np.linalg.norm(fn, axis=1)[:, None]
    e = np.concatenate([T[:, [0, 1]], T[:, [1, 2]], T[:, [2, 0]]])
    f = np.tile(np.arange(len(T)), 3)
    k = np.minimum(e[:, 0], e[:, 1]) * len(V) + np.maximum(e[:, 0], e[:, 1])
    o = np.argsort(k, kind="stable")
    k, f = k[o], f[o]
    same = k[1:] == k[:-1]
    fa, fb = f[:-1][same], f[1:][same]
    ew = arr["info"]["wall"]
    thr = ew + 0.45 * (V[:, 2].max() - ew)              # the upper 55 % above the side wall
    m = (P[:, :, 2].min(1)[fa] > thr) & (P[:, :, 2].min(1)[fb] > thr)
    if xmax is not None:                                # the middle of a strip (its square ends are corners)
        m &= (np.abs(P[:, :, 0].max(1))[fa] < xmax) & (np.abs(P[:, :, 0].max(1))[fb] < xmax)
    ang = np.degrees(np.arccos(np.clip((fn[fa] * fn[fb]).sum(1), -1, 1)))[m]
    return float(np.percentile(ang, 99)), float(ang.max())

out = {{}}
# a stroke narrower than 2 x bevel (t 0.16, b 0.08, half width 0.03): a ROUND TUBE of radius 0.03 — HEAD built a roof
# ridge along its spine (hb(0.03; 0.08) with slope 1.25 there)
for tag, k in (("strip", 0.0), ("strip_k", 0.25)):
    arr = H.build([G.rect_spline(-0.4, -0.03, 0.4, 0.03)], 0.16, 0.08, k, 8, 1.0)
    V = arr["verts"]
    mid = V[(np.abs(V[:, 0]) < 0.2) & (V[:, 2] > 0)]
    e = arr["info"]["wall"]
    out[tag] = dict(folds=folds(arr, 0.3), half=arr["info"]["half"], wall=e,
                    spine=float(mid[np.abs(mid[:, 1]) < 0.004, 2].mean()) - e, **H.check_arrays(arr))
# corpus pieces (the user's examples of the roof ridge) at their import depth
for name, spl, t, bev, k, S in json.loads(open({shapes!r}).read()):
    arr = H.build(spl, t, bev, k, 8, S)
    out[name] = dict(folds=folds(arr), **H.check_arrays(arr))
open({out!r}, "w").write(json.dumps(out))
"""

    def test_thin_strokes_are_round_tubes(corpus_index, tmp_path):
        """PLAN §11 round 8 (QA r9 N4): the round edge is capped at each part's LOCAL half-width — a stroke narrower than
        2 × bevel is a round tube (no roof ridge on its spine); Syno Photos' rings and Ti84's keys at their import depth
        keep their dome folds low (HEAD: p99 58° / 74°)."""
        index_path, _names = corpus_index
        index = json.loads(index_path.read_text(encoding="utf-8"))
        shapes = []
        for name, lid, k in (("Syno Photos", None, 0), ("Ti84", "L3", 5)):
            proj = json.loads(Path(index[name]["project"]).read_text(encoding="utf-8"))
            bundle = json.loads(Path(index[name]["geometryPath"]).read_text(encoding="utf-8"))
            L = [x for x in proj["layers"] if lid is None or x["id"] == lid][0]
            g = bundle["layers"][L["id"]]
            S = float(proj["canvas"]["art"].get("scale", 1.0)) * float((L.get("transform") or {}).get("scale", 1.0))
            dp = L["depth"]
            shapes.append([name, g["regions"][k]["splines"], dp["thickness"] / S,
                           min(dp["bevel"], dp["thickness"] / 2) / S, dp.get("inflate", 0.0), S])
        sp = tmp_path / "shapes.json"
        sp.write_text(json.dumps(shapes), encoding="utf-8")
        out = tmp_path / "tube.json"
        expr = _TUBE_EXPR.format(repo=str(REPO), out=str(out), shapes=str(sp))
        p = subprocess.run([str(wc.BLENDER), "-b", "--factory-startup", "--python-expr", expr],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        assert out.is_file(), p.stdout[-3000:] + p.stderr[-3000:]
        r = json.loads(out.read_text(encoding="utf-8"))
        for v in r.values():
            assert v["nonManifold"] == 0 and v["misoriented"] == 0 and v["invertedNormals"] == 0 and v["volume"] > 0
        s = r["strip"]
        # a tube of radius 0.03 on the wall (the local half-width is measured with RING_TOL: up to ~4 % generous)
        assert s["half"] == pytest.approx(s["wall"] + 0.03, rel=0.06), s
        assert s["spine"] == pytest.approx(0.03, rel=0.06), s
        assert s["folds"][1] < 25.0, s                # HEAD: a 77° ridge; now one 16-gon tube step (~11°) per row
        assert r["strip_k"]["folds"][1] < 25.0, r["strip_k"]
        assert r["Syno Photos"]["folds"][0] < 48.0, r["Syno Photos"]
        assert r["Ti84"]["folds"][0] < 45.0, r["Ti84"]

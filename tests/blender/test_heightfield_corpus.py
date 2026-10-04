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

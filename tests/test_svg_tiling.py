"""Round 4: tiled art -> one 'combined' layer, edge linings, full-bleed flag, plate-rim snapping.

QA round 3: Home's white seams + inset green bar (#10), the seam between Maps' yellow and green
(#13), Gmail's shading wedge as a clear sliver (#5), Classroom / CRD frames with a white / green
hairline at the plate edge and a grey inner line (#2)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import shapely
from shapely.geometry import Polygon

from conftest import CORPUS_DIR, SVGTESTS_DIR

import bis.svg as svg
from bis.svg import tiling
from bis.svg.geometry import layer_regions, members_of
from bis.svg.plate import _outline_points


def corpus(name: str) -> Path:
    return CORPUS_DIR / f"{name}.svg"


def _layers(project):
    return [(L.name, L.mode, len(L.elementIds)) for L in project.layers]


# ----------------------------------------------------------------------------------------------
# tiles of one shape share a 'combined' layer
# ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,n", [("Home", 4), ("Maps", 5), ("Drive", 6), ("Play Store", 11)])
def test_tiled_shapes_are_one_combined_layer(name, n, import_icon):
    """The coloured sections of one shape (cut along straight edges) are ONE layer built as one
    combined body: no V groove / white seam between them and no step (Home's green bar)."""
    res, project, pdir = import_icon(corpus(name))
    assert len(project.layers) == 1, _layers(project)
    L = project.layers[0]
    assert L.mode == "combined" and len(L.elementIds) == n
    bundle = svg.build_geometry(pdir, project, "/files/projects/p", texture_size=64)
    lg = bundle.layers[L.id]
    # the body is the union silhouette: one outer contour (+ Maps' hole), not a contour per piece
    outers = [s for s in lg.silhouette if not s.hole]
    assert len(outers) == 1, len(outers)
    assert L.depth.bevel <= 0.9 * lg.safeRadius + 1e-4


@pytest.mark.parametrize("name,layer", [("Gmail", 0), ("Google Calendar", 0), ("CRD", 1), ("CRD", 2),
                                        ("Wallet", 1), ("Earth", 3), ("DJI", 0)])
def test_layers_with_seams_inside_are_combined(name, layer, import_icon):
    """Regions of one layer that share much of their outline (Gmail's shading wedges, the
    Calendar's body pieces, the halves of CRD's chevrons, Wallet's window strips) -> combined."""
    res, project, pdir = import_icon(corpus(name))
    L = project.layers[layer]
    assert L.mode == "combined", _layers(project)
    store = svg.read_store(pdir)
    m = tiling.region_tiling(layer_regions(store, members_of(store, L.elementIds)), store.tolerance,
                             store.art.k)
    assert m["ratio"] >= tiling.COMBINED_MIN_RATIO and m["shared"] >= tiling.COMBINED_MIN_LEN, m


@pytest.mark.parametrize("name,n", [("Calculator", 2), ("Photos", 4), ("Stack", 3), ("Canvas", 2)])
def test_separate_pieces_stay_individual(name, n, import_icon):
    """Separate pieces (Calculator's symbols, Canvas's dots), petals in their own layers and objects
    lying on each other (Stack's plates: their shared edge is a plate CORNER, not a cut) keep their
    planes and their own bodies."""
    res, project, pdir = import_icon(corpus(name))
    assert len(project.layers) == n, _layers(project)
    assert all(L.mode == "individual" for L in project.layers), _layers(project)


def test_stacked_waves_and_inlays_keep_their_planes(import_icon):
    """Earth's waves butt along CURVES (one wave lies on the next): still 4 planes. Wallet's window
    is set INTO a notch of the card (an inlay): card and window keep separate planes."""
    res, project, pdir = import_icon(corpus("Earth"))
    assert len(project.layers) == 4
    assert [L.mode for L in project.layers] == ["individual"] * 3 + ["combined"]
    res, project, pdir = import_icon(corpus("Wallet"))
    assert [(L.name, L.mode) for L in project.layers] == [("White", "individual"), ("Blue", "combined")]


def test_see_through_layers_stay_individual(import_icon):
    """The combined body is painted from the layer texture without its alpha: a layer with a
    translucent region over nothing (Secure Folder's 66 % white tab) stays individual."""
    res, project, pdir = import_icon(corpus("Secure Folder"))
    tab = project.layers[0]
    assert len(tab.elementIds) == 2 and tab.mode == "individual"
    store = svg.read_store(pdir)
    m = tiling.region_tiling(layer_regions(store, members_of(store, tab.elementIds)), store.tolerance,
                             store.art.k)
    assert m["seeThrough"] and m["ratio"] >= tiling.COMBINED_MIN_RATIO   # it does tile ...


TILED = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <path id="left" d="M20 20H50V80H20Z" fill="#e53935"/>
  <path id="right" d="M50 20H80V80H50Z" fill="#1e88e5"/>
  <circle id="dot" cx="88" cy="10" r="6" fill="#43a047"/>
</svg>"""

OVERLAY = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <g id="badge">
    <path id="body" d="M20 20H80V80H20Z" fill="#e53935"/>
    <path id="shade" d="M50 20H80V80H50Z" fill="#000000" fill-opacity="0.25"/>
  </g>
</svg>"""

GHOST = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <g id="badge">
    <path id="body" d="M20 20H50V80H20Z" fill="#e53935"/>
    <path id="glass" d="M50 20H80V80H50Z" fill="#ffffff" fill-opacity="0.5"/>
  </g>
</svg>"""


def test_synthetic_tiles_overlay_and_see_through(import_icon):
    res, project, pdir = import_icon(TILED, name="tiled.svg")
    combined = [L for L in project.layers if L.mode == "combined"]
    assert len(combined) == 1 and len(combined[0].elementIds) == 2, _layers(project)
    assert all(len(L.elementIds) == 1 and L.mode == "individual" for L in project.layers if L not in combined)
    # a translucent shading region lying on another region of its layer is paint: combined
    res, project, pdir = import_icon(OVERLAY, "single", name="overlay.svg")
    assert _layers(project) == [(project.layers[0].name, "combined", 2)]
    # ... a translucent region over NOTHING would turn opaque on a combined body: individual
    res, project, pdir = import_icon(GHOST, "single", name="ghost.svg")
    assert project.layers[0].mode == "individual"


SLIT = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <path id="left" d="M20 20H49.999V80H20Z" fill="#e53935"/>
  <path id="right" d="M50 20H80V80H50Z" fill="#1e88e5"/>
</svg>"""


def test_combined_silhouette_bridges_rounding_slits(import_icon):
    """Sections that butt only up to rounding (Drive: 0.001-unit slits from 3-decimal coordinates)
    must still give ONE combined body - two bodies would show the seam again."""
    res, project, pdir = import_icon(SLIT, name="slit.svg")
    assert len(project.layers) == 1 and project.layers[0].mode == "combined", _layers(project)
    bundle = svg.build_geometry(pdir, project, "/files/projects/p", texture_size=32)
    sil = bundle.layers[project.layers[0].id].silhouette
    assert len(sil) == 1 and not sil[0].hole


def test_tile_pairs_need_a_straight_cut(import_icon):
    res, project, pdir = import_icon(TILED, "element", name="tiled.svg")
    store = svg.read_store(pdir)
    fg = list(range(len(store.elems)))
    pairs = tiling.tile_pairs(store.elems, fg, store.gaps, store.tolerance, store.art.k)
    assert [(store.elems[i].id, store.elems[j].id) for i, j, _ in pairs] == [("e0", "e1")]
    assert pairs[0][2] == pytest.approx(1.2, abs=0.02)   # the 60-unit cut = 1.2 art units
    # Stack's plates butt along a plate corner (two long runs): not a cut
    res, project, pdir = import_icon(corpus("Stack"))
    store = svg.read_store(pdir)
    fg = [i for i, e in enumerate(store.elems) if e.id not in store.plate_ids]
    assert tiling.tile_pairs(store.elems, fg, store.gaps, store.tolerance, store.art.k) == []


# ----------------------------------------------------------------------------------------------
# structural edits re-derive the default mode (but keep a mode the user picked)
# ----------------------------------------------------------------------------------------------
def test_edits_rederive_default_modes(import_icon):
    res, project, pdir = import_icon(corpus("Home"), "element")
    assert len(project.layers) == 4 and all(L.mode == "individual" for L in project.layers)
    layers = svg.merge_layers(pdir, project, [L.id for L in project.layers])
    assert len(layers) == 1 and layers[0].mode == "combined"
    project.layers = layers
    pieces = svg.split_layer(pdir, project, layers[0].id, "elements")
    assert len(pieces) == 4 and all(L.mode == "individual" for L in pieces)
    # a mode the user picked survives a merge
    res, project, pdir = import_icon(corpus("Photos"))
    project.layers[1].mode = "combined"
    layers = svg.merge_layers(pdir, project, [project.layers[1].id, project.layers[2].id])
    assert layers[1].mode == "combined"
    res, project, pdir = import_icon(corpus("Photos"))
    layers = svg.merge_layers(pdir, project, [project.layers[1].id, project.layers[2].id])
    assert layers[1].mode == "individual"   # petals touch at a point only: separate bodies


def test_move_rederives_modes(import_icon):
    res, project, pdir = import_icon(corpus("Home"))
    L = project.layers[0]
    green = next(e for e in project.elements if e.name.lower().startswith("green") or e.id == L.elementIds[-1])
    layers = svg.move_elements(pdir, project, [green.id], None)
    assert len(layers) == 2
    for layer in layers:
        expect = "combined" if len(layer.elementIds) > 1 else "individual"
        assert layer.mode == expect, _layers(project)


# ----------------------------------------------------------------------------------------------
# debris never gets a layer of its own
# ----------------------------------------------------------------------------------------------
def test_zero_area_slivers_join_a_neighbour(import_icon):
    """Play Store has 7 zero-area red slivers; once its sections form one tile layer they used to
    end up as a layer of their own (bevel 1e-05)."""
    res, project, pdir = import_icon(corpus("Play Store"))
    assert len(project.layers) == 1 and project.layers[0].depth.bevel > 0.01


# ----------------------------------------------------------------------------------------------
# linings: an edge line drawn under a frame belongs to the frame
# ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["Classroom", "CRD"])
def test_frame_lining_joins_the_frame(name, import_icon):
    """Classroom's yellow / CRD's grey frame has a stroke ring under its inner edge whose visible
    rest is a 0.02-wide band. On its own plane it showed as a grey groove line inside the frame
    (QA r3 #2); now frame + lining are one combined layer at the bottom of the stack."""
    res, project, pdir = import_icon(corpus(name))
    frame = project.layers[0]
    assert frame.mode == "combined" and len(frame.elementIds) == 2, _layers(project)
    store = svg.read_store(pdir)
    kinds = sorted(store.get(e).role for e in frame.elementIds)
    assert kinds == ["fill", "stroke"]
    assert frame.depth.z == 0.0


# ----------------------------------------------------------------------------------------------
# SourceInfo.fullBleed
# ----------------------------------------------------------------------------------------------
def test_full_bleed_flag(import_icon):
    res, project, pdir = import_icon(corpus("Earth"))
    assert res.source.fullBleed and not res.source.plateDetected
    res, project, pdir = import_icon(corpus("Home"))
    assert not res.source.fullBleed and res.source.plateDetected
    res, project, pdir = import_icon(SVGTESTS_DIR / "c_holes_evenodd.svg")
    assert not res.source.fullBleed


# ----------------------------------------------------------------------------------------------
# art flush with the plate edge reaches it (no plate-coloured hairline)
# ----------------------------------------------------------------------------------------------
def _silhouette_union(bundle, canvas):
    from test_svg_geometry import _ring

    s, tx, ty = canvas.art.scale, canvas.art.x, canvas.art.y
    return shapely.union_all([Polygon(_ring(sp, 8) * s + (tx, ty)).buffer(0)
                              for lg in bundle.layers.values() for sp in lg.silhouette if not sp.hole])


@pytest.mark.parametrize("name", ["Classroom", "CRD", "DJI", "Earth"])
def test_flush_art_is_snapped_onto_the_plate_outline(name, import_icon):
    """The 3D art used to stop 0.004 inside the plate outline - plus up to 0.007 where the source
    template's outline strays inside the fitted shape: a 0.006 (mean) hairline of plate around
    every frame / full-bleed icon. The band 0.0015..0.008 inside the outline was 35 % covered;
    now art within PLATE_SNAP of the outline is grown onto it."""
    res, project, pdir = import_icon(corpus(name))
    bundle = svg.build_geometry(pdir, project, "/files/projects/p", texture_size=64)
    c = res.canvas
    plate = Polygon(_outline_points(c.shape, c.cornerRadius, (0, 0), 1.0, 0.0, 512))
    u = _silhouette_union(bundle, c)
    band = plate.buffer(-0.0015).difference(plate.buffer(-0.008))
    assert band.intersection(u).area / band.area > 0.99, name
    assert u.difference(plate.buffer(3e-4)).area < 1e-6, name


def test_snapping_leaves_art_inside_the_plate_alone(import_icon):
    """Art far from the plate edge is untouched by the rim snap (Spotify's waves, Home's house)."""
    from bis.svg import geometry as G

    for name in ("Spotify", "Home"):
        res, project, pdir = import_icon(corpus(name))
        store = svg.read_store(pdir)
        clip, band, snap = G.plate_clip(store)
        for L in project.layers:
            regs = [p for _m, p in G.occlusion_regions(members_of(store, L.elementIds))]
            snapped = G.snap_to_rim(regs, band, snap)
            assert all(a is b for a, b in zip(regs, snapped)), name


# ----------------------------------------------------------------------------------------------
# corpus: exactly these layers switch to 'combined' (thresholds chosen on this list)
# ----------------------------------------------------------------------------------------------
EXPECTED_COMBINED = {
    "Classroom": ["Yellow Gradient"], "CRD": ["Gray Gradient", "Red & Yellow", "Green & Blue"],
    "DJI": ["Dark Gray & Black"], "Drive": ["Green, Blue +2"], "Earth": ["Light Blue & White"],
    "Files_1": ["Blue, Red +2"], "Find Device": ["Blue & White"], "Gmail": ["Red"],
    "Google Calendar": ["Blue"], "Home": ["Blue, Red +2"], "Maps": ["Green, Blue +2"],
    "Play Store": ["Green, Red +2"], "Ti73": ["Gray & Black"], "Ti84": ["White, Light Gray +4"],
    "Translate": ["Blue & Pink"], "Wallet": ["Blue"],
}


@pytest.mark.corpus
def test_corpus_combined_layers(import_icon):
    got = {}
    ratios_c, ratios_i = [], []
    for path in sorted(CORPUS_DIR.glob("*.svg")):
        res, project, pdir = import_icon(path)
        names = [L.name for L in project.layers if L.mode == "combined"]
        if names:
            got[path.stem] = names
        store = svg.read_store(pdir)
        for L in project.layers:
            if len(L.elementIds) < 2:
                continue
            m = tiling.region_tiling(layer_regions(store, members_of(store, L.elementIds)), store.tolerance,
                                     store.art.k)
            if m["seeThrough"] or m["overlay"]:
                continue
            (ratios_c if L.mode == "combined" else ratios_i).append(m["ratio"])
    assert got == EXPECTED_COMBINED
    # the threshold sits in a wide empty gap of the corpus distribution
    assert max(ratios_i) < 0.05 < tiling.COMBINED_MIN_RATIO < 0.15 < min(ratios_c), (max(ratios_i), min(ratios_c))


# ----------------------------------------------------------------------------------------------
# review r4: the region-adjacency query scales (STRtree instead of an O(n^2) distance loop)
# ----------------------------------------------------------------------------------------------
def test_region_neighbours_match_brute_force():
    rng = np.random.default_rng(7)
    geoms = [shapely.Point(x, y).buffer(r) for x, y, r in rng.uniform([0, 0, 0.5], [50, 50, 2], (250, 3))]
    for eps in (0.0, 0.3, 1.5):
        brute = [[b for b in range(len(geoms)) if b != a and geoms[b].distance(geoms[a]) <= eps]
                 for a in range(len(geoms))]
        assert tiling._neighbours(geoms, eps) == brute


def test_region_tiling_many_regions_is_fast(import_icon):
    """440 separate dots on one layer: the mode metrics used to take ~1.5 s (pairwise distances),
    paid on import and again on every merge / move / split of the layer."""
    import time

    cols = ["#e53935", "#1e88e5", "#43a047", "#fdd835"]
    dots = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">' + "".join(
        f'<circle cx="{6 + (k % 22) * 4}" cy="{6 + (k // 22) * 4}" r="1.2" fill="{cols[k % 4]}"/>'
        for k in range(440)) + "</svg>").encode()
    res, project, pdir = import_icon(dots, "single", name="dots.svg")
    assert project.layers[0].mode == "individual"
    store = svg.read_store(pdir)
    regions = layer_regions(store, members_of(store, project.layers[0].elementIds))
    t = time.perf_counter()
    m = tiling.region_tiling(regions, store.tolerance, store.art.k)
    assert time.perf_counter() - t < 0.5
    assert m["regions"] == 440 and m["shared"] == 0.0 and not m["overlay"] and not m["seeThrough"]

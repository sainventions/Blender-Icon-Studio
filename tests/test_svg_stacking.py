"""Round 7 (PLAN §11): ``LayerGeometry.maxRadius`` (max inscribed radius of the bodies the worker builds),
real-height stacking of the import defaults and of structural edits, the thickness/2-only bevel clamp, and the
plate fill's pad-only end stops (Twitter's red sliver)."""
from __future__ import annotations

import pytest

from conftest import CORPUS_DIR

import bis.svg as svg
from bis import stacking
from bis.models import FillLinear, GradientStop
from bis.svg.geometry import layer_max_radius
from bis.svg.layers import DEFAULT_BEVEL, DEFAULT_INFLATE, DEFAULT_THICKNESS
from bis.svg.plate import trim_pad_stops

K = 2.0 / 500.0   # art units per SVG unit (500 x 500 viewBox)

DISC_RING = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 500 500">
  <circle id="disc" cx="150" cy="250" r="100" fill="#ff0000"/>
  <path id="ring" fill="#0000ff" fill-rule="evenodd"
        d="M450 250a100 100 0 1 0-200 0a100 100 0 1 0 200 0z M410 250a60 60 0 1 0-120 0a60 60 0 1 0 120 0z"/>
</svg>"""

# two opaque halves of one 300 x 200 slab, butting along x = 250
HALVES = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 500 500">
  <rect id="left" x="100" y="150" width="150" height="200" fill="#ff0000"/>
  <rect id="right" x="250" y="150" width="150" height="200" fill="#0000ff"/>
</svg>"""

STACK = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 500 500">
  <rect id="base" x="60" y="60" width="380" height="380" rx="40" fill="#1d4ed8"/>
  <circle id="mid" cx="250" cy="250" r="120" fill="#f59e0b"/>
  <rect id="top" x="200" y="200" width="100" height="100" fill="#ffffff"/>
</svg>"""


def _bundle(pdir, project):
    return svg.build_geometry(pdir, project, "/files/projects/p", texture_size=64)


def _radii(pdir, project):
    return {lid: lg.maxRadius for lid, lg in _bundle(pdir, project).layers.items()}


def _gaps(pdir, project, layers=None):
    p = project.model_copy(deep=True)
    if layers is not None:
        p.layers = layers
    return stacking.stack_gaps(p.layers, _radii(pdir, p), p.canvas.art.scale)


# ---------------------------------------------------------------------------------------------- maxRadius
def test_max_radius_of_disc_and_ring(import_icon):
    res, project, pdir = import_icon(DISC_RING, "element")
    lid = {res.elements[i].origId: L.id for L in project.layers for i in range(len(res.elements))
           if res.elements[i].id in L.elementIds}
    b = _bundle(pdir, project)
    assert b.layers[lid["disc"]].maxRadius == pytest.approx(100 * K, abs=1e-3)       # the disc's own radius
    assert b.layers[lid["ring"]].maxRadius == pytest.approx(20 * K, abs=1e-3)        # (100 - 60) / 2: holes count
    # the same numbers without building geometry (import / structural edits use this)
    store = svg.read_store(pdir)
    for L in project.layers:
        assert layer_max_radius(store, L.elementIds, L.mode) == pytest.approx(b.layers[L.id].maxRadius, abs=1e-6)


def test_max_radius_follows_the_bodies_the_worker_builds(import_icon):
    """'combined' and touching opaque pieces are ONE silhouette body (the worker's touching_opaque): D is the
    whole slab's; translucent pieces stay separate bodies: D is a half's."""
    res, project, pdir = import_icon(HALVES, "single")
    (L,) = project.layers
    for mode in ("combined", "individual"):
        L.mode = mode
        assert _bundle(pdir, project).layers[L.id].maxRadius == pytest.approx(100 * K, abs=1e-3), mode
    see_through = HALVES.replace(b'fill="#0000ff"', b'fill="#0000ff" fill-opacity="0.5"')
    res2, p2, d2 = import_icon(see_through, "single")
    p2.layers[0].mode = "individual"
    assert _bundle(d2, p2).layers[p2.layers[0].id].maxRadius == pytest.approx(75 * K, abs=1e-3)


@pytest.mark.parametrize("name", ["Ti84", "Maps", "Find Device", "Gmail", "Contacts"])
def test_import_radius_matches_the_bundle(name, import_icon):
    res, project, pdir = import_icon(CORPUS_DIR / f"{name}.svg")
    b = _bundle(pdir, project)
    store = svg.read_store(pdir)
    for L in project.layers:
        lg = b.layers[L.id]
        assert lg.maxRadius > 0
        assert layer_max_radius(store, L.elementIds, L.mode) == pytest.approx(lg.maxRadius, abs=1e-6)
    # ... so the imported stack is exactly the real-height stack for the bundle's radii
    assert _gaps(pdir, project) == pytest.approx([stacking.geometry_rules()["stackGap"]] * (len(project.layers) - 1),
                                                 abs=2e-4)


def test_max_radius_ignores_zero_width_cracks_and_spikes():
    """Union silhouettes of touching pieces keep hairline cracks and needle spikes along the shared edges (Maps,
    Wallet, Earth). The exported splines / the worker's outline cleaning drop them, so they must not shrink D:
    measured with them, Maps' pin came out 0.167 instead of 0.213 and its dome rose 0.025 above H."""
    from shapely.geometry import Polygon
    from bis.svg.geometry import max_radius

    sq = [(0, 0), (1, 0), (1, 1), (0, 1)]
    crack = Polygon(sq, [[(0.5, 0.002), (0.5004, 0.5), (0.5, 0.998), (0.4996, 0.5)]])   # 0.0008 wide, 0.996 long
    notch = Polygon([(0, 0), (0.4998, 0), (0.5, 0.7), (0.5002, 0), (1, 0), (1, 1), (0, 1)])   # needle notch
    for geom in (crack, notch):
        assert max_radius([geom], close=0.0) < 0.36                 # the hairline splits / pinches the square
        assert max_radius([geom]) == pytest.approx(0.5, abs=2e-3)   # ... but not the body the worker builds
    # a real gap between two islands is kept (closing works per island) and a real hole still counts
    two = Polygon([(0, 0), (0.4, 0), (0.4, 1), (0, 1)]).union(Polygon([(0.41, 0), (1, 0), (1, 1), (0.41, 1)]))
    assert max_radius([two]) == pytest.approx(0.295, abs=2e-3)
    ring = Polygon(sq, [[(0.3, 0.3), (0.7, 0.3), (0.7, 0.7), (0.3, 0.7)]])
    assert max_radius([ring]) == pytest.approx(max_radius([ring], close=0.0), abs=1e-3)


def _worker_heightfield():
    """The worker's height-field module (pure numpy at import) - the contract check below measures D the way
    the bodies are really built. Skipped when the worker package is not importable."""
    import sys
    root = str(CORPUS_DIR.parent)
    if root not in sys.path:
        sys.path.append(root)
    try:
        from blender_worker import heightfield
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"blender_worker.heightfield not importable: {e}")
    return heightfield


def test_max_radius_bounds_the_worker_inradius_of_combined_bodies(import_icon):
    """H = thickness + 2 x inflate x maxRadius must never be lower than the body the worker builds: for every
    'combined' (silhouette) body of the icons whose union silhouettes carry cracks, maxRadius >= the worker's own
    inradius of the exported silhouette (and not wildly above it)."""
    hf = _worker_heightfield()
    checked = 0
    for name in ("Maps", "Wallet", "Earth", "Illinois"):
        res, project, pdir = import_icon(CORPUS_DIR / f"{name}.svg")
        variants = [project.layers]
        if len(project.layers) > 1:   # the merged base layers (Earth's globe: D was 0.287 instead of 0.505)
            variants.append(svg.merge_layers(pdir, project, [project.layers[0].id, project.layers[1].id]))
        for layers in variants:
            p = project.model_copy(deep=True)
            p.layers = layers
            b = _bundle(pdir, p)
            S = p.canvas.art.scale
            for L in p.layers:
                if L.mode != "combined":
                    continue
                g = b.layers[L.id]
                d_worker = hf.inradius([s.model_dump() for s in g.silhouette], S)
                assert g.maxRadius >= d_worker - 1e-3, (name, L.id, g.maxRadius, d_worker)
                assert g.maxRadius <= d_worker + 0.04, (name, L.id, g.maxRadius, d_worker)
                checked += 1
    assert checked >= 5


# ---------------------------------------------------------------------------------------------- import defaults
def test_import_defaults_and_real_height_stack(import_icon):
    res, project, pdir = import_icon(STACK)
    assert len(project.layers) == 2 and res.source.plateDetected     # base = plate; mid + top = layers
    rules = stacking.geometry_rules()
    radii = _radii(pdir, project)
    z = rules["stackLift"]
    for L in project.layers:
        d = L.depth
        assert (d.thickness, d.bevel, d.inflate) == (DEFAULT_THICKNESS, DEFAULT_BEVEL, DEFAULT_INFLATE)
        assert d.bevel == d.thickness / 2                                 # fully rounded (pill) edge
        assert L.material.preset == "liquid_glass" and L.shadow.kind == "physical"
        assert d.z == pytest.approx(z, abs=1e-5)
        z += d.thickness + 2 * d.inflate * radii[L.id] * project.canvas.art.scale + rules["stackGap"]
    # the circle's body (r = 120 SVG units, dome inflate x D) is far taller than the old 0.13 step
    assert project.layers[1].depth.z > 0.3


# ---------------------------------------------------------------------------------------------- structural edits
def test_edits_restack_a_real_height_stack(import_icon):
    res, project, pdir = import_icon(CORPUS_DIR / "Photos.svg")
    gap = stacking.geometry_rules()["stackGap"]
    merged = svg.merge_layers(pdir, project, [project.layers[1].id, project.layers[2].id])
    assert _gaps(pdir, project, merged) == pytest.approx([gap] * (len(merged) - 1), abs=2e-4)
    resplit = svg.split_layers(pdir, project, "element")
    assert _gaps(pdir, project, resplit) == pytest.approx([gap] * (len(resplit) - 1), abs=2e-4)
    moved = svg.move_elements(pdir, project, [project.layers[3].elementIds[0]], None)
    assert _gaps(pdir, project, moved) == pytest.approx([gap] * (len(moved) - 1), abs=2e-4)


def test_edits_keep_a_looks_gap(import_icon):
    """A stack with another uniform gap (a look's zGap) keeps that gap through structural edits."""
    res, project, pdir = import_icon(CORPUS_DIR / "Photos.svg")
    radii = _radii(pdir, project)
    stacking.restack(project.layers, radii, gap=0.1, art_scale=project.canvas.art.scale)
    merged = svg.merge_layers(pdir, project, [project.layers[2].id, project.layers[3].id])
    assert _gaps(pdir, project, merged) == pytest.approx([0.1] * (len(merged) - 1), abs=2e-4)


def test_edits_keep_a_hand_placed_stack_but_never_interpenetrate(import_icon):
    res, project, pdir = import_icon(CORPUS_DIR / "Photos.svg")
    for i, L in enumerate(project.layers):
        L.depth.z = round(0.5 * i ** 1.5, 4)                       # hand-placed: 0, 0.5, 1.414, 2.598
    custom = [L.depth.z for L in project.layers]
    split = svg.merge_layers(pdir, project, [project.layers[1].id, project.layers[2].id])
    assert [L.depth.z for L in split] == pytest.approx([custom[0], custom[1], custom[3]])   # nothing moved
    # pieces of a split layer all start at its z: each is lifted onto the one below (real heights), and the
    # layers above make room only where they would intersect
    project.layers = split
    pieces = svg.split_layer(pdir, project, split[1].id, "elements")
    assert len(pieces) == 4
    assert min(_gaps(pdir, project, pieces)) >= -1e-6
    assert pieces[0].depth.z == pytest.approx(custom[0]) and pieces[1].depth.z == pytest.approx(custom[1])


def test_legacy_default_stack_restacks_on_edit(import_icon):
    """A project saved with the pre-round-7 default stack (z = i x 0.13) is treated as a default stack."""
    res, project, pdir = import_icon(CORPUS_DIR / "Photos.svg")
    for i, L in enumerate(project.layers):
        L.depth.z = round(i * 0.13, 6)
        L.depth.thickness, L.depth.bevel, L.depth.inflate = 0.10, 0.045, 0.0
    merged = svg.merge_layers(pdir, project, [project.layers[2].id, project.layers[3].id])
    gap = stacking.geometry_rules()["stackGap"]
    assert _gaps(pdir, project, merged) == pytest.approx([gap] * (len(merged) - 1), abs=2e-4)


def test_template_layers_clamp_bevel_to_half_thickness(import_icon):
    res, project, pdir = import_icon(CORPUS_DIR / "Ti84.svg")
    src = project.layers[-1]
    src.depth.thickness, src.depth.bevel = 0.12, 0.2                  # a stale / pasted over-large bevel
    layers = svg.split_layer(pdir, project, src.id, "elements")
    assert all(L.depth.bevel <= L.depth.thickness / 2 + 1e-9 for L in layers)
    assert {L.depth.bevel for L in layers if L.depth.thickness == 0.12} == {0.06}


# ---------------------------------------------------------------------------------------------- plate fill stops
def test_trim_pad_stops():
    def lin(*stops):
        return FillLinear(stops=[GradientStop(offset=o, color=c) for o, c in stops])

    tw = trim_pad_stops(lin((0, "#1d9bf0"), (1, "#1a6ed4"), (1, "#bd4012")))
    assert [(s.offset, s.color) for s in tw.stops] == [(0, "#1d9bf0"), (1, "#1a6ed4")]
    lead = trim_pad_stops(lin((0, "#ff0000"), (0, "#00ff00"), (0.5, "#0000ff"), (1, "#000000")))
    assert [s.color for s in lead.stops] == ["#00ff00", "#0000ff", "#000000"]
    ok = lin((0, "#ff0000"), (0.5, "#00ff00"), (0.5, "#0000ff"), (1, "#000000"))   # a mid hard stop is art
    assert trim_pad_stops(ok) is ok
    assert trim_pad_stops(lin((1, "#ff0000"), (1, "#00ff00"))).stops[0].color == "#ff0000"   # never empty
    assert trim_pad_stops(None) is None


def test_twitter_plate_has_no_red_end_stop(import_icon):
    res, project, pdir = import_icon(CORPUS_DIR / "Twitter.svg")
    fill = res.canvas.plate.fill
    assert fill.type == "linear"
    assert [s.color for s in fill.stops] == ["#1d9bf0", "#1a6ed4"]
    assert "#bd4012" not in {s.color for s in fill.stops}

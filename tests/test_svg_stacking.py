"""Rounds 7 + 8 (PLAN §11): ``LayerGeometry.maxRadius`` (max inscribed radius of the bodies the worker builds),
real-height OVERLAP-AWARE stacking of the import defaults and of structural edits (a layer only stacks above the lower
layers it overlaps in XY; H includes the in-layer stack of overlapping pieces), raster image layers as flat cards,
the thickness/2-only bevel clamp, and the plate fill's pad-only end stops (Twitter's red sliver)."""
from __future__ import annotations

import pytest

from conftest import CORPUS_DIR, assert_rule_stack

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

# round 8: two discs side by side (`apart` SVG units between their edges) and a dot on the left one
SIDE = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 500 500">
  <circle id="left" cx="150" cy="250" r="80" fill="#ff0000"/>
  <circle id="right" cx="{rx}" cy="250" r="80" fill="#0000ff"/>
  <circle id="dot" cx="150" cy="250" r="30" fill="#ffffff"/>
</svg>"""

# a translucent glass disc over an opaque slab (one layer: two pieces that OVERLAP), a dot above them both
GLASS = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 500 500">
  <rect id="slab" x="40" y="120" width="300" height="200" rx="30" fill="#2244aa"/>
  <circle id="glass" cx="300" cy="260" r="110" fill="#ffffff" fill-opacity="0.5"/>
  <circle id="dot" cx="300" cy="260" r="25" fill="#ff2200"/>
</svg>"""


def _bundle(pdir, project):
    return svg.build_geometry(pdir, project, "/files/projects/p", texture_size=64)


def _radii(pdir, project):
    return {lid: lg.maxRadius for lid, lg in _bundle(pdir, project).layers.items()}


def _check(pdir, project, layers=None, gap=None):
    """`layers` (default: the project's) form the overlap-aware real-height stack for the bundle's geometry."""
    p = project.model_copy(deep=True)
    if layers is not None:
        p.layers = layers
    return assert_rule_stack(p, _bundle(pdir, p), gap)


def _by_orig(res, layers):
    ids = {e.id: e.origId for e in res.elements}
    return {ids[L.elementIds[0]]: L for L in layers}


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
    # ... so the imported stack is exactly the overlap-aware real-height stack for the bundle's geometry
    _check(pdir, project)


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
    _check(pdir, project)   # the square lies on the circle: overlap-aware == sequential here


@pytest.mark.parametrize("apart,stacked", [(40, False), (4, True)])
def test_side_by_side_layers_share_the_base(apart, stacked, import_icon):
    """Round 8: a layer only stacks above the lower layers it overlaps in XY (footprints closer than the clearance
    stackGap = 0.03 art units ~ 7.5 SVG units here): the right disc sits on the base next to the left one unless
    they (nearly) touch; the dot always stacks on the left disc only."""
    res, project, pdir = import_icon(SIDE.format(rx=150 + 160 + apart).encode(), "element")
    gap = stacking.geometry_rules()["stackGap"]
    L = _by_orig(res, project.layers)
    assert set(L) == {"left", "right", "dot"}
    shapes = _check(pdir, project)
    S = project.canvas.art.scale
    H = dict(zip([l.id for l in project.layers], stacking.heights(project.layers, shapes, S)))
    assert L["left"].depth.z == 0.0
    assert L["right"].depth.z == pytest.approx(H[L["left"].id] + gap if stacked else 0.0, abs=1e-4)
    assert L["dot"].depth.z == pytest.approx(H[L["left"].id] + gap, abs=1e-4)      # on the left disc only
    # the stack top is lower than the round-7 sequential stack (every layer on its lower neighbour)
    top = max(l.depth.z + H[l.id] for l in project.layers)
    seq = stacking.stack_z([H[l.id] for l in project.layers], gap)
    assert top < seq[-1] + H[project.layers[-1].id] - 0.1


def test_in_layer_overlap_raises_the_layer_height(import_icon):
    """Round 8: H = max(rule height, in-layer stacked height). A translucent disc over an opaque slab in ONE
    'individual' layer is stacked on it by the worker (scene._relations / heightfield.stack_shifts), so the layer
    is taller than thickness + 2 x inflate x maxRadius - the dot's layer above clears that real top. The server's
    port agrees with the worker's own height-field functions."""
    res, project, pdir = import_icon(GLASS, "element")
    L = _by_orig(res, project.layers)
    merged = svg.merge_layers(pdir, project, [L["slab"].id, L["glass"].id])
    base = next(l for l in merged if set(l.elementIds) == set(L["slab"].elementIds) | set(L["glass"].elementIds))
    base.mode = "individual"
    project.layers = merged
    stacking.restack(project.layers, svg.layer_shapes(pdir, project), art_scale=project.canvas.art.scale)
    shapes = _check(pdir, project)
    S = project.canvas.art.scale
    sh = shapes[base.id]
    assert sh.pairs() == [(0, 1)]                                     # the glass overlaps the slab
    rule = stacking.body_height(base.depth, sh.maxRadius, S)
    H = stacking.layer_height(base, sh, S)
    assert H > rule + 0.1
    top = next(l for l in project.layers if l is not base)
    assert top.depth.z == pytest.approx(base.depth.z + H + stacking.geometry_rules()["stackGap"], abs=5e-4)
    # store-derived shapes (what the import / edits stack with) give the same height
    store_sh = svg.layer_shapes(pdir, project)[base.id]
    assert stacking.layer_height(base, store_sh, S) == pytest.approx(H, abs=2e-3)
    # 'combined': one silhouette body - the rule height only
    base.mode = "combined"
    assert stacking.layer_height(base, sh, S) == pytest.approx(rule)
    base.mode = "individual"
    # the worker's own functions (blender_worker.heightfield, pure numpy) on the exported region splines
    hf = _worker_heightfield()
    g = _bundle(pdir, project).layers[base.id]
    spl = [[s.model_dump() for s in r.splines] for r in g.regions]
    rings = [hf.piece_rings(x, S) for x in spl]
    assert hf.rings_relation(rings[0], rings[1], stacking.TOUCH_TOL / S) == 2
    d = base.depth
    hs = [hf.half_height(d.thickness, min(d.bevel, d.thickness / 2), d.inflate, hf.inradius(x, S) * S) for x in spl]
    sw = hf.stack_shifts(2, [(0, 1)], hs, stacking.PIECE_STACK_GAP)
    H_worker = float(max(a + b for a, b in zip(sw, hs)) + max(hs))
    assert H == pytest.approx(H_worker, abs=0.01)
    assert stacking.half_height(0.16, 0.08, 0.25, 0.3) == pytest.approx(hf.half_height(0.16, 0.08, 0.25, 0.3))


# ---------------------------------------------------------------------------------------------- structural edits
@pytest.mark.parametrize("name", ["Photos", "Lens"])
def test_edits_restack_a_real_height_stack(name, import_icon):
    res, project, pdir = import_icon(CORPUS_DIR / f"{name}.svg")
    merged = svg.merge_layers(pdir, project, [project.layers[1].id, project.layers[2].id])
    _check(pdir, project, merged)
    resplit = svg.split_layers(pdir, project, "element")
    _check(pdir, project, resplit)
    moved = svg.move_elements(pdir, project, [project.layers[3].elementIds[0]], None)
    _check(pdir, project, moved)


def test_edits_keep_a_looks_gap(import_icon):
    """A stack with another uniform gap (a look's zGap) keeps that gap through structural edits."""
    res, project, pdir = import_icon(CORPUS_DIR / "Photos.svg")
    stacking.restack(project.layers, svg.layer_shapes(pdir, project), gap=0.1, art_scale=project.canvas.art.scale)
    _check(pdir, project, gap=0.1)
    merged = svg.merge_layers(pdir, project, [project.layers[2].id, project.layers[3].id])
    _check(pdir, project, merged, gap=0.1)


def test_round7_sequential_stack_converts_on_edit(import_icon):
    """A project saved with the round-7 rule (every layer stacked on its lower neighbour, whether they overlap or
    not) is recognised as a rule stack: the next structural edit re-stacks it overlap-aware with the same gap."""
    res, project, pdir = import_icon(SIDE.format(rx=350).encode(), "element")
    L = _by_orig(res, project.layers)
    radii = {lid: sh.maxRadius for lid, sh in svg.layer_shapes(pdir, project).items()}
    stacking.restack(project.layers, radii, art_scale=project.canvas.art.scale)   # bare radii: sequential
    assert L["right"].depth.z > 0.3
    shapes = svg.layer_shapes(pdir, project)
    assert stacking.stack_gap(project.layers, shapes, art_scale=project.canvas.art.scale) == pytest.approx(0.03)
    merged = svg.merge_layers(pdir, project, [L["left"].id, L["dot"].id])
    assert len(merged) == 2 and [l.depth.z for l in merged] == [0.0, 0.0]          # side by side on the base
    _check(pdir, project, merged)


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
    p = project.model_copy(deep=True)
    p.layers = pieces
    shapes = stacking.shapes_from_bundle(_bundle(pdir, p), pieces, p.canvas.art.scale)
    assert stacking.interpenetrations(pieces, shapes, art_scale=p.canvas.art.scale) == []
    assert stacking.stack_gap(pieces, shapes, art_scale=p.canvas.art.scale) is None          # still hand-placed
    assert pieces[0].depth.z == pytest.approx(custom[0]) and pieces[1].depth.z == pytest.approx(custom[1])


def test_legacy_default_stack_restacks_on_edit(import_icon):
    """A project saved with the pre-round-7 default stack (z = i x 0.13) is treated as a default stack."""
    res, project, pdir = import_icon(CORPUS_DIR / "Photos.svg")
    for i, L in enumerate(project.layers):
        L.depth.z = round(i * 0.13, 6)
        L.depth.thickness, L.depth.bevel, L.depth.inflate = 0.10, 0.045, 0.0
    merged = svg.merge_layers(pdir, project, [project.layers[2].id, project.layers[3].id])
    _check(pdir, project, merged)


# ---------------------------------------------------------------------------------------------- raster cards
@pytest.mark.parametrize("name", ["Find Device", "iMessage", "Vanced Neon"])
def test_raster_image_layers_import_as_flat_cards(name, import_icon):
    """Round 8: a layer of raster images only is a flat card (no dome, 0.02 thick, round edge 0.006) - Find
    Device's traced shine image used to become a 0.38-tall glass dome; vector layers keep the defaults."""
    res, project, pdir = import_icon(CORPUS_DIR / f"{name}.svg")
    kinds = {e.id: e.kind for e in project.elements}
    cards = [L for L in project.layers if stacking.is_image_layer(L, kinds)]
    assert cards
    for L in project.layers:
        want = ((stacking.IMAGE_CARD["thickness"], stacking.IMAGE_CARD["bevel"], 0.0) if L in cards
                else (DEFAULT_THICKNESS, DEFAULT_BEVEL, DEFAULT_INFLATE))
        assert (L.depth.thickness, L.depth.bevel, L.depth.inflate) == want, (name, L.name)
    shapes = _check(pdir, project)
    for L, H in zip(project.layers, stacking.heights(project.layers, shapes, project.canvas.art.scale)):
        if L in cards:
            assert H == pytest.approx(stacking.IMAGE_CARD["thickness"])
    # an image moved out of a vector layer becomes a card too (Find Device: the shine into a new layer)
    if name == "Find Device":
        img = next(L for L in cards)
        project.layers = svg.merge_layers(pdir, project, [project.layers[1].id, img.id])
        moved = svg.move_elements(pdir, project, list(img.elementIds), None)
        new = next(L for L in moved if L.elementIds == img.elementIds)
        assert (new.depth.thickness, new.depth.inflate) == (stacking.IMAGE_CARD["thickness"], 0.0)


def test_cards_across_structural_edits(import_icon):
    """A flat card merged with (or receiving) vector art gets a full body back - the vector layer's depth, never a
    0.02 card that flattens the vector art; a layer left with raster images only becomes a card; cards merged
    together stay a card. The stack stays the overlap-aware rule stack."""
    res, project, pdir = import_icon(CORPUS_DIR / "Find Device.svg")
    kinds = {e.id: e.kind for e in project.elements}
    card = next(L for L in project.layers if stacking.is_image_layer(L, kinds))
    k = [L.id for L in project.layers].index(card.id)
    vec = project.layers[k + 1]                                   # the vector layer right above the card
    full = (vec.depth.thickness, vec.depth.bevel, vec.depth.inflate)
    flat = (stacking.IMAGE_CARD["thickness"], stacking.IMAGE_CARD["bevel"], 0.0)

    def depth(L):
        return L.depth.thickness, L.depth.bevel, L.depth.inflate

    merged = svg.merge_layers(pdir, project, [card.id, vec.id])
    m = next(L for L in merged if card.elementIds[0] in L.elementIds)
    assert depth(m) == full
    _check(pdir, project, merged)
    moved = svg.move_elements(pdir, project, [vec.elementIds[0]], card.id)     # vector art into the card
    t = next(L for L in moved if L.id == card.id)
    assert depth(t) == full
    _check(pdir, project, moved)
    # a vector layer that keeps only its raster image becomes a card
    below = project.layers[k - 1]
    project.layers = svg.merge_layers(pdir, project, [below.id, card.id])
    mixed = next(L for L in project.layers if card.elementIds[0] in L.elementIds)
    assert depth(mixed) == (below.depth.thickness, below.depth.bevel, below.depth.inflate)
    out = svg.move_elements(pdir, project, [e for e in mixed.elementIds if kinds[e] != "image"], None)
    left = next(L for L in out if L.elementIds == card.elementIds)
    assert depth(left) == flat
    _check(pdir, project, out)
    # cards merged together stay a card
    res, vn, vdir = import_icon(CORPUS_DIR / "Vanced Neon.svg")
    one = svg.merge_layers(vdir, vn, [L.id for L in vn.layers])
    assert len(one) == 1 and depth(one[0]) == flat


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

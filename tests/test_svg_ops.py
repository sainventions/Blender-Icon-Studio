"""Layer operations (re-split / merge / split / move) with z-order validation, and the geometry cache."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from conftest import CORPUS_DIR, SVGTESTS_DIR

import bis.svg as svg
from bis.svg import ZOrderError

# A (red) under B (blue) under C (red): A and C overlap only through B -> "weaving" if merged.
WEAVE = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect id="A" x="10" y="10" width="40" height="40" fill="#ff0000"/>
  <rect id="B" x="30" y="30" width="40" height="40" fill="#0000ff"/>
  <rect id="C" x="50" y="50" width="40" height="40" fill="#ff0000"/>
  <circle id="D" cx="85" cy="15" r="8" fill="#00aa00"/>
</svg>"""

# one compound path with three islands + a separate circle (so import does not explode it)
ISLANDS = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <path id="dots" fill="#222222" d="M10 10h15v15h-15z M40 10h15v15h-15z M70 10h15v15h-15z"/>
  <circle id="ball" cx="50" cy="70" r="20" fill="#f59e0b"/>
</svg>"""


def ids_of(layers):
    return [list(L.elementIds) for L in layers]


def assert_real_stack(pdir, project, layers, gap=None):
    """`layers` form the real-height stack (PLAN 11 round 7) for the max radii the geometry bundle reports."""
    from bis import stacking

    p = project.model_copy(deep=True)
    p.layers = layers
    bundle = svg.build_geometry(pdir, p, "/files/projects/p", texture_size=64)
    radii = {lid: lg.maxRadius for lid, lg in bundle.layers.items()}
    rules = stacking.geometry_rules()
    assert layers[0].depth.z == pytest.approx(rules["stackLift"], abs=1e-5)
    gaps = stacking.stack_gaps(layers, radii, project.canvas.art.scale)
    want = rules["stackGap"] if gap is None else gap
    assert gaps == pytest.approx([want] * len(gaps), abs=2e-4), gaps


def test_resplit_strategies_exclude_plate(import_icon):
    res, project, pdir = import_icon(CORPUS_DIR / "Maps.svg")
    plate_ids = {e.id for e in res.elements} - {i for L in res.layers for i in L.elementIds}
    for strategy in ("smart", "group", "color", "element", "single"):
        layers = svg.split_layers(pdir, project, strategy)
        got = [i for L in layers for i in L.elementIds]
        assert not plate_ids & set(got), strategy
        assert sorted(got) == sorted(i for L in res.layers for i in L.elementIds), strategy
    assert len(svg.split_layers(pdir, project, "single")) == 1


def test_merge_legal_keeps_bottom_layer_identity(import_icon):
    res, project, pdir = import_icon(CORPUS_DIR / "Photos.svg")
    L1, L3 = project.layers[0], project.layers[2]
    project.layers[0].material.preset = "jelly"
    layers = svg.merge_layers(pdir, project, [L3.id, L1.id])
    assert len(layers) == len(res.layers) - 1
    merged = next(L for L in layers if L.id == L1.id)
    assert set(merged.elementIds) == set(L1.elementIds) | set(L3.elementIds)
    assert merged.material.preset == "jelly"
    assert_real_stack(pdir, project, layers)                          # the default stack is re-stacked
    assert all(L.id != L3.id for L in layers)


def test_merge_weaving_is_refused(import_icon):
    res, project, pdir = import_icon(WEAVE, "element")
    by_name = {e.origId: e.id for e in res.elements}
    lay = {i: L.id for L in project.layers for i in L.elementIds}
    with pytest.raises(ZOrderError):
        svg.merge_layers(pdir, project, [lay[by_name["A"]], lay[by_name["C"]]])
    # but A + D (no overlaps) is fine
    layers = svg.merge_layers(pdir, project, [lay[by_name["A"]], lay[by_name["D"]]])
    assert len(layers) == len(project.layers) - 1
    with pytest.raises(ValueError):
        svg.merge_layers(pdir, project, [project.layers[0].id])
    with pytest.raises(ValueError):
        svg.merge_layers(pdir, project, [project.layers[0].id, "L99"])


def test_move_elements(import_icon):
    res, project, pdir = import_icon(WEAVE, "single")
    by_name = {e.origId: e.id for e in res.elements}
    assert len(project.layers) == 1
    # move C to a new layer -> 2 layers, C on top (it is painted last and overlaps B)
    layers = svg.move_elements(pdir, project, [by_name["C"]], None)
    assert len(layers) == 2 and layers[-1].elementIds == [by_name["C"]]
    project.layers = layers
    # moving B into C's layer leaves A below: legal
    layers2 = svg.move_elements(pdir, project, [by_name["B"]], layers[1].id)
    assert ids_of(layers2) == [[by_name["A"], by_name["D"]], [by_name["B"], by_name["C"]]]
    # moving A out alone while B stays with... A + C together with B separate -> weaving
    project.layers = layers
    with pytest.raises(ZOrderError):
        svg.move_elements(pdir, project, [by_name["A"]], layers[1].id)
    # moving everything into one layer removes the emptied layer
    layers3 = svg.move_elements(pdir, project, [by_name["C"]], layers[0].id)
    assert len(layers3) == 1
    with pytest.raises(ValueError):
        svg.move_elements(pdir, project, ["nope"], None)


def test_split_layer_elements_mode(import_icon):
    res, project, pdir = import_icon(WEAVE, "single")
    layers = svg.split_layer(pdir, project, project.layers[0].id, "elements")
    assert len(layers) == 4
    assert layers[0].id == project.layers[0].id
    assert_real_stack(pdir, project, layers)
    # stacking respects paint order where things overlap
    order = [L.elementIds[0] for L in layers]
    ids = {e.origId: e.id for e in res.elements}
    assert order.index(ids["A"]) < order.index(ids["B"]) < order.index(ids["C"])
    with pytest.raises(ValueError):
        svg.split_layer(pdir, project, "L42", "elements")


def test_split_layer_islands_mode_updates_elements(import_icon):
    res, project, pdir = import_icon(ISLANDS, "single")
    dots = next(e.id for e in res.elements if e.origId == "dots")
    layers = svg.split_layer(pdir, project, project.layers[0].id, "islands")
    new_ids = [e.id for e in project.elements]
    assert dots not in new_ids and {f"{dots}-1", f"{dots}-2", f"{dots}-3"} <= set(new_ids)
    assert sorted(i for L in layers for i in L.elementIds) == sorted(new_ids)
    assert len(layers) == 4
    # the store follows: geometry for the new layers builds fine
    project.layers = layers
    bundle = svg.build_geometry(pdir, project, "/files/projects/p", texture_size=64)
    assert set(bundle.layers) == {L.id for L in layers}
    store = svg.read_store(pdir)
    # the exploded element is superseded, not deleted (an undone split still references it)
    assert store.replaced[dots] == [f"{dots}-1", f"{dots}-2", f"{dots}-3"] and dots in store.index
    # a re-split of the new state never brings the superseded element back
    resplit = svg.split_layers(pdir, project, "element")
    assert sorted(i for L in resplit for i in L.elementIds) == sorted(new_ids)


def test_islands_split_survives_undo(import_icon):
    """The web UI undoes a structural op by PUTting the previous project.json back: every element
    it references must still resolve - geometry, merge/move and re-split must not lose art."""
    res, project, pdir = import_icon(ISLANDS, "element")
    before = project.model_copy(deep=True)
    dots = next(e.id for e in res.elements if e.origId == "dots")
    lid = next(L.id for L in project.layers if dots in L.elementIds)
    after = project.model_copy(deep=True)
    after.layers = svg.split_layer(pdir, after, lid, "islands")
    assert dots not in {e.id for e in after.elements}
    undone = before  # what the server gets back after Ctrl+Z
    bundle = svg.build_geometry(pdir, undone, "/files/projects/p", texture_size=64)
    lg = bundle.layers[lid]
    assert len([s for s in lg.silhouette if not s.hole]) == 3  # all three dots still there
    merged = svg.merge_layers(pdir, undone, [L.id for L in undone.layers])
    assert sorted(i for L in merged for i in L.elementIds) == sorted(e.id for e in res.elements)
    resplit = svg.split_layers(pdir, undone, "element")
    assert sorted(i for L in resplit for i in L.elementIds) == sorted(e.id for e in res.elements)
    moved = svg.move_elements(pdir, undone, [dots], None)
    assert any(L.elementIds == [dots] for L in moved)
    # redo-by-repeating: exploding again reuses the same island ids (no duplicates in the store)
    again = before.model_copy(deep=True)
    again.layers = svg.split_layer(pdir, again, lid, "islands")
    assert {e.id for e in again.elements} == {e.id for e in after.elements}
    store = svg.read_store(pdir)
    assert len(store.elems) == len(res.elements) + 3


def test_split_single_shape_layer_raises(import_icon):
    res, project, pdir = import_icon(b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
                                     b'<rect width="10" height="10" fill="#fff"/>'
                                     b'<circle cx="5" cy="5" r="3" fill="red"/></svg>')
    lid = project.layers[0].id
    with pytest.raises(ValueError):
        svg.split_layer(pdir, project, lid, "elements")


def test_geometry_cache_hit_is_fast_and_per_layer(import_icon):
    # (Maps used to be 4 layers; since round 4 its tiled pin is one 'combined' layer)
    res, project, pdir = import_icon(CORPUS_DIR / "Photos.svg")
    b1 = svg.build_geometry(pdir, project, "/files/projects/p", texture_size=256)
    t = time.perf_counter()
    for _ in range(10):
        b2 = svg.build_geometry(pdir, project, "/files/projects/p", texture_size=256)
    assert (time.perf_counter() - t) / 10 < 0.05
    assert b2.hash == b1.hash
    # a cold process (no memory cache) still hits the disk cache quickly
    from bis.svg import geometry as G

    G._BUNDLES.clear()
    t = time.perf_counter()
    b3 = svg.build_geometry(pdir, project, "/files/projects/p", texture_size=256)
    assert time.perf_counter() - t < 0.05 and b3 == b1
    # changing one layer only rebuilds that layer
    project.layers = svg.merge_layers(pdir, project, [project.layers[2].id, project.layers[3].id])
    b4 = svg.build_geometry(pdir, project, "/files/projects/p", texture_size=256)
    assert b4.hash != b1.hash
    for lid in (project.layers[0].id, project.layers[1].id):
        assert b4.layers[lid].texture == b1.layers[lid].texture
        assert b4.layers[lid].hash == b1.layers[lid].hash
    # layer settings that do not change geometry (material, depth) keep the bundle hash
    project.layers[0].material.preset = "chrome"
    project.layers[0].depth.thickness = 0.3
    assert svg.build_geometry(pdir, project, "/files/projects/p", texture_size=256).hash == b4.hash
    # 'combined' mode changes the safe radius basis -> new hash for that layer only
    project.layers[0].mode = "combined"
    b5 = svg.build_geometry(pdir, project, "/files/projects/p", texture_size=256)
    assert b5.layers[project.layers[0].id].hash != b4.layers[project.layers[0].id].hash
    assert b5.layers[project.layers[1].id].hash == b4.layers[project.layers[1].id].hash


def test_auto_names_follow_membership(import_icon):
    res, project, pdir = import_icon(CORPUS_DIR / "Photos.svg")
    names = [L.name for L in project.layers]
    assert names == ["Blue", "Green", "Yellow", "Red"]
    layers = svg.merge_layers(pdir, project, [project.layers[1].id, project.layers[2].id])
    assert layers[1].name in ("Green & Yellow", "Yellow & Green")
    # a user-renamed layer keeps its name
    project.layers[1].name = "My red"
    layers = svg.merge_layers(pdir, project, [project.layers[1].id, project.layers[2].id])
    assert layers[1].name == "My red"


def test_strategy_group_on_inkscape(import_icon):
    res, project, pdir = import_icon(SVGTESTS_DIR / "j_inkscape_layers.svg", "group")
    assert [L.name for L in res.layers] == ["Face", "Shine"]

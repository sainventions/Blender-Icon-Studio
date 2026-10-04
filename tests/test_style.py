"""Looks, style extraction and style transfer (PLAN §10) — pure functions + the REST endpoints.

Unit tests need nothing; the API tests use the FakeBridge and the real ``bis.svg`` pipeline (corpus icons) so the
real-height stack (PLAN §11 round 7) is checked against real ``LayerGeometry.maxRadius`` values.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from fastapi.testclient import TestClient  # noqa: E402

from bis.blender import FakeBridge  # noqa: E402
from bis.main import create_app  # noqa: E402
from bis.models import (  # noqa: E402
    CameraSpec,
    Element,
    FillSolid,
    FillSystem,
    Layer,
    LayerDepth,
    LayerOverride,
    Lighting,
    MaterialSpec,
    Project,
    SourceInfo,
    StyleSpec,
    Tint,
)
from bis.materials import clean_params, param_schema  # noqa: E402
from bis.style import (  # noqa: E402
    LookNotFound,
    StyleError,
    apply_style,
    clamp_bevel,
    clean_style,
    deep_merge,
    extract_style,
    resolve_look,
    resolve_style_request,
)
from bis.models import StyleRequest  # noqa: E402
from bis import stacking  # noqa: E402
from bis.testing import make_test_settings  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
PRESETS = json.loads((ROOT / "shared" / "presets.json").read_text(encoding="utf-8"))
GAP = stacking.geometry_rules(PRESETS)["stackGap"]
LIFT = stacking.geometry_rules(PRESETS)["stackLift"]


def _real_svg() -> bool:
    try:
        import bis.svg as s

        return all(hasattr(s, n) for n in ("import_svg", "build_geometry", "geometry_path"))
    except Exception:
        return False


def _project(n_layers: int = 3, **kw) -> Project:
    layers = [Layer(id=f"l{i}", name=f"Layer {i}", elementIds=[f"e{i}"],
                    depth=LayerDepth(z=0.2 * i, thickness=0.08, bevel=0.03))
              for i in range(n_layers)]
    return Project(id="p", name="P", createdAt="t", updatedAt="t",
                   source=SourceInfo(filename="p.svg", viewBox=(0, 0, 100, 100)), layers=layers, **kw)


# ---------------------------------------------------------------------------------------------- looks
@pytest.mark.parametrize("look_id", sorted(PRESETS["looks"]))
def test_every_look_resolves(look_id):
    look = PRESETS["looks"][look_id]
    style = resolve_look(PRESETS, look_id)
    assert isinstance(style, StyleSpec)
    want = look["style"]
    assert style.layerDefaults.material.preset == want["layerDefaults"]["material"]["preset"]
    assert style.layerDefaults.material.preset in PRESETS["materials"]
    assert style.plate.material.preset in PRESETS["materials"]
    if "lighting" in want:
        assert style.lighting is not None and style.lighting.preset == want["lighting"]["preset"]
        assert style.lighting.preset in PRESETS["lighting"]
    assert style.zGap == want.get("zGap", StyleSpec().zGap)
    # round 5: looks never carry a layer mode - each icon keeps its art-derived ('combined' for tiles) mode
    assert "mode" not in want["layerDefaults"] and style.layerDefaults.mode is None


def test_resolve_look_merges_over_defaults_and_rejects_unknown():
    style = resolve_look(PRESETS, "crystal")
    assert style.layerDefaults.material.preset == "clear_glass"
    assert style.layerDefaults.material.params == {"tint": 0.35}
    assert style.zGap == 0.05 and style.lighting.preset == "darkfield"   # round 8: a clearance, not a z step
    assert style.plate.fill is None and style.camera is None          # not in the look → keep the icon's own
    neon = resolve_look(PRESETS, "neon")
    assert neon.plate.fill is not None and neon.plate.fill.type == "system-dark"
    partial = {"looks": {"x": {"label": "X", "style": {"layerDefaults": {"depth": {"bevel": 0.07}},
                                                       "lighting": {"angle": 30}}}}}
    s = resolve_look(partial, "x")
    assert s.layerDefaults.depth.bevel == 0.07 and s.layerDefaults.depth.thickness == 0.10   # default kept
    assert s.layerDefaults.material.preset == "liquid_glass"
    assert s.lighting.angle == 30 and s.lighting.preset == "studio"                       # Lighting defaults
    with pytest.raises(LookNotFound):
        resolve_look(PRESETS, "nope")
    assert deep_merge({"a": {"b": 1, "c": 2}, "l": [1]}, {"a": {"b": 3}, "l": [2]}) == {"a": {"b": 3, "c": 2}, "l": [2]}


# ---------------------------------------------------------------------------------------------- apply
def test_apply_style_rules():
    p = _project(3)
    p.canvas.plate.fill = FillSolid(color="#123456")
    p.appearances.dark.layers["l1"] = LayerOverride(material=MaterialSpec(preset="chrome"), opacity=0.5)
    p.layers[0].elementMaterials = {"e0": MaterialSpec(preset="neon")}
    style = StyleSpec.model_validate({
        "layerDefaults": {"material": {"preset": "candy", "params": {"tint": 0.4}},
                          "depth": {"z": 9, "thickness": 0.11, "bevel": 0.05, "bevelSegments": 8, "inflate": 0.2},
                          "shadow": {"kind": "chromatic", "opacity": 0.6}, "mode": "combined"},
        "zGap": 0.15,
        "plate": {"material": {"preset": "glossy_plastic"}, "thickness": 0.2, "bevel": 0.05},
        "lighting": {"preset": "dramatic", "angle": -30},
        "camera": {"view": "front", "iso": 0.4, "zoom": 1.1, "explode": 3.0},
        "colorMode": "agx",
        "tint": {"color": "#ff0000", "strength": 0.5},
    })
    out = apply_style(p, style, {"l0": 0.01, "l1": 0.5}, presets=PRESETS)
    assert p.layers[0].material.preset == "liquid_glass" and p.layers[0].depth.z == 0.0  # input untouched
    assert [l.material.preset for l in out.layers] == ["candy"] * 3
    assert out.layers[0].material.params == {"tint": 0.4}
    # PLAN 11 round 7: real-height stack, H = thickness + 2 x inflate x maxRadius, gap = zGap (0.15); l2 has no
    # radius and no elements (bbox bound 0)
    h0, h1 = 0.11 + 2 * 0.2 * 0.01, 0.11 + 2 * 0.2 * 0.5
    assert [l.depth.z for l in out.layers] == pytest.approx([LIFT, LIFT + h0 + 0.15, LIFT + h0 + h1 + 0.30])
    assert all(l.depth.bevel == 0.05 for l in out.layers)            # <= thickness/2: no safe-radius clamp
    assert all(l.depth.thickness == 0.11 and l.depth.bevelSegments == 8 and l.depth.inflate == 0.2
               for l in out.layers)
    pill = StyleSpec.model_validate({"layerDefaults": {"depth": {"thickness": 0.11, "bevel": 0.09}}})
    assert {l.depth.bevel for l in apply_style(p, pill).layers} == {0.055}   # clamped to thickness/2 only
    # PLAN 11: only real shadows - the legacy art-directed 'chromatic' kind is applied as 'physical'
    assert all(l.shadow.kind == "physical" and l.shadow.opacity == 0.6 and l.mode == "combined"
               for l in out.layers)
    assert all(l.elementMaterials == {} for l in out.layers)          # the look shows on every shape too
    assert p.layers[0].elementMaterials == {"e0": MaterialSpec(preset="neon")}
    assert out.canvas.plate.material.preset == "glossy_plastic"
    assert (out.canvas.plate.thickness, out.canvas.plate.bevel) == (0.2, 0.05)
    assert out.canvas.plate.fill == FillSolid(color="#123456")      # fill None → keep
    assert out.canvas.shape == "squircle"                            # shape None → keep
    assert out.lighting.preset == "dramatic" and out.lighting.angle == -30
    assert out.camera.iso == 0.4 and out.camera.zoom == 1.1     # the CAD-style POV travels with the style...
    assert out.camera.explode == 1.0                              # ...but layers are never spread (real distances)
    assert out.render.colorMode == "agx" and out.appearances.tint == Tint(color="#ff0000", strength=0.5)
    ov = out.appearances.dark.layers["l1"]
    assert ov.material is None and ov.opacity == 0.5                 # the look shows in every appearance

    keep = apply_style(p, StyleSpec(zGap=None, plate={"fill": {"type": "system-dark"}, "shape": "circle"}),
                       presets=PRESETS)
    # zGap None → the presets' stackGap (StyleSpec default depth: thickness 0.10, inflate 0)
    assert [l.depth.z for l in keep.layers] == pytest.approx([LIFT + i * (0.10 + GAP) for i in range(3)])
    assert keep.canvas.plate.fill == FillSystem(type="system-dark") and keep.canvas.shape == "circle"
    assert keep.lighting == p.lighting and keep.camera == p.camera and keep.render.colorMode == p.render.colorMode


def test_style_without_mode_keeps_each_layers_mode():
    """QA round 4 #8: a look (mode None) never turns a tiling-derived 'combined' layer back into 'individual'
    (and never fuses an 'individual' one); the result stays a valid Project (round-4 HEAD wrote mode null)."""
    p = _project(3)
    p.layers[0].mode = "combined"
    p.layers[2].mode = "combined"
    for look_id in sorted(PRESETS["looks"]):
        out = apply_style(p, resolve_look(PRESETS, look_id))
        assert [l.mode for l in out.layers] == ["combined", "individual", "combined"], look_id
        Project.model_validate(out.model_dump(mode="json"))          # never null
    forced = apply_style(p, StyleSpec(layerDefaults={"mode": "individual"}))
    assert {l.mode for l in forced.layers} == {"individual"}           # an explicit mode still applies
    assert [l.mode for l in p.layers] == ["combined", "individual", "combined"]   # input untouched


def test_extracted_mode_only_when_the_user_set_it():
    p = _project(3)
    auto = {"l0": "combined", "l1": "individual", "l2": "individual"}
    p.layers[0].mode = "combined"
    assert extract_style(p).layerDefaults.mode is None                       # auto modes unknown → never copied
    assert extract_style(p, auto_modes=auto).layerDefaults.mode is None      # all art-derived (mixed)
    for l in p.layers:
        l.mode = "combined"                                                  # the user fused l1 + l2
    assert extract_style(p, auto_modes=auto).layerDefaults.mode == "combined"
    assert extract_style(p).layerDefaults.mode is None
    p.layers[1].mode = "individual"                                          # mixed → None
    assert extract_style(p, auto_modes=auto).layerDefaults.mode is None
    for l in p.layers:
        l.mode = "individual"   # the user split l0's body - or a pre-round-4 project: never copied (QA r4 #8)
    assert extract_style(p, auto_modes=auto).layerDefaults.mode is None
    q = _project(2)
    for l in q.layers:
        l.mode = "combined"
    assert extract_style(q, auto_modes={"l0": "combined", "l1": "combined"}).layerDefaults.mode is None  # art-derived
    assert extract_style(q, auto_modes={"zz": "individual"}).layerDefaults.mode is None   # unknown ids don't count
    q.layers[1].visible = False
    q.layers[1].mode = "individual"                                          # hidden layers are not the look
    assert extract_style(q, auto_modes={"l0": "individual", "l1": "individual"}).layerDefaults.mode == "combined"


def test_layer_materials_by_index_clamped():
    style = StyleSpec(layerMaterials=[MaterialSpec(preset="satin"), MaterialSpec(preset="neon")])
    out = apply_style(_project(4), style)
    assert [l.material.preset for l in out.layers] == ["satin", "neon", "neon", "neon"]
    assert apply_style(_project(0), style).layers == []


def test_clamp_bevel():
    """Round 7 (QA defect 7): the bevel is clamped to thickness/2 only - no safe-radius clamp."""
    assert clamp_bevel(0.045, 0.1) == 0.045
    assert clamp_bevel(0.07, 0.14) == 0.07                            # a full pill edge stays a pill
    assert clamp_bevel(0.045, 0.02) == 0.01
    assert clamp_bevel(0.045, 0.0) == 0.0
    assert clamp_bevel(-0.01, 0.1) == 0.0
    assert clamp_bevel(0.05, 0.04807) == 0.02403 <= 0.04807 / 2      # rounded down, never above the limit


def test_real_height_stack_formula():
    """H = thickness + 2 x inflate x maxRadius x S (S = canvas.art.scale x layer.transform.scale);
    z0 = stackLift, z(i+1) = z(i) + H(i) + gap (zGap, else the presets' stackGap)."""
    p = _project(3)
    p.canvas.art.scale = 1.25
    p.layers[1].transform.scale = 0.5
    style = StyleSpec.model_validate({"layerDefaults": {"depth": {"thickness": 0.16, "bevel": 0.08, "inflate": 0.25}},
                                      "zGap": None})
    presets = {"geometry": {"stackLift": 0.01, "stackGap": 0.05}}
    out = apply_style(p, style, {"l0": 0.4, "l1": 0.2, "l2": 0.1}, presets=presets)
    h0 = 0.16 + 2 * 0.25 * 0.4 * 1.25
    h1 = 0.16 + 2 * 0.25 * 0.2 * 1.25 * 0.5
    assert [l.depth.z for l in out.layers] == pytest.approx([0.01, 0.01 + h0 + 0.05, 0.01 + h0 + h1 + 0.10])
    assert stacking.stack_gaps(out.layers, {"l0": 0.4, "l1": 0.2, "l2": 0.1}, 1.25) == pytest.approx([0.05, 0.05])
    assert stacking.stack_gap(out.layers, {"l0": 0.4, "l1": 0.2, "l2": 0.1}, art_scale=1.25,
                              presets=presets) == pytest.approx(0.05)
    # zGap given overrides stackGap; missing radii fall back to a bbox bound (never under-estimated)
    p.elements = [Element(id=f"e{i}", name=f"e{i}", paint=FillSolid(), bbox=(-0.3, -0.2, 0.3, 0.2), area=0.06)
                  for i in range(3)]
    out = apply_style(p, style.model_copy(update={"zGap": 0.0}), presets=presets)
    hb = 0.16 + 2 * 0.25 * 0.2 * 1.25                                  # bbox bound: half the smaller side 0.4
    assert out.layers[1].depth.z == pytest.approx(0.01 + hb)
    assert stacking.geometry_rules({}) == {"stackLift": 0.0, "stackGap": 0.03}     # defaults without a section
    assert stacking.geometry_rules(PRESETS) == {"stackLift": LIFT, "stackGap": GAP}


def test_liquid_glass_look_matches_the_import_defaults():
    """N6 (PLAN 11 round 8): the Liquid Glass look is the import default body - thickness 0.16, bevel 0.08,
    inflate 0.25, gap = stackGap - and every look's gap is null (stackGap) or a small clearance <= 0.05."""
    from bis.svg.layers import DEFAULT_BEVEL, DEFAULT_INFLATE, DEFAULT_SEGMENTS, DEFAULT_THICKNESS

    lg = resolve_look(PRESETS, "liquid-glass")
    d = lg.layerDefaults.depth
    assert (d.thickness, d.bevel, d.inflate, d.bevelSegments) == (DEFAULT_THICKNESS, DEFAULT_BEVEL, DEFAULT_INFLATE,
                                                                  DEFAULT_SEGMENTS)
    assert lg.zGap is None and lg.layerDefaults.material.preset == "liquid_glass"
    assert lg.layerDefaults.shadow.kind == "physical"
    for look in PRESETS["looks"]:
        st = resolve_look(PRESETS, look)
        assert st.zGap is None or 0.0 < st.zGap <= 0.05, look
        assert st.layerDefaults.depth.bevel <= st.layerDefaults.depth.thickness / 2, look


def _img_project() -> Project:
    """Two vector layers and a raster image layer on top."""
    p = _project(3)
    p.elements = [Element(id="e0", name="e0", paint=FillSolid(), bbox=(-0.5, -0.5, 0.5, 0.5), area=0.25),
                  Element(id="e1", name="e1", paint=FillSolid(), bbox=(-0.2, -0.2, 0.2, 0.2), area=0.04),
                  Element(id="e2", name="e2", kind="image", role="image", paint=FillSolid(),
                          bbox=(-0.6, -0.6, 0.6, 0.6), area=0.36)]
    return p


def test_looks_keep_raster_layers_flat_cards():
    """Round 8: raster image layers are flat cards under every look - no dome, at most 0.02 thick, a small round
    edge - while vector layers take the look's depth; Copy style never takes its depth from a card."""
    p = _img_project()
    for look in PRESETS["looks"]:
        st = resolve_look(PRESETS, look)
        out = apply_style(p, st, {"l0": 0.5, "l1": 0.2, "l2": 0.5}, presets=PRESETS)
        card = out.layers[2].depth
        assert card.inflate == 0.0 and card.thickness <= stacking.IMAGE_CARD["thickness"], look
        assert card.bevel <= min(stacking.IMAGE_CARD["bevel"], card.thickness / 2), look
        want = st.layerDefaults.depth
        assert (out.layers[0].depth.thickness, out.layers[0].depth.inflate) == (want.thickness, want.inflate), look
        assert out.layers[2].material == out.layers[0].material                  # the look's material still applies
    q = apply_style(p, resolve_look(PRESETS, "clay"), {"l0": 0.5, "l1": 0.2, "l2": 0.5}, presets=PRESETS)
    assert extract_style(q).layerDefaults.depth.inflate == 0.3                    # not the card's 0


def test_looks_give_crisp_raster_layers_a_body():
    """Round 9: only SOFT-alpha rasters (Element.softAlpha True; None = imported before round 9, as imported) stay
    flat cards under a look; a CRISP raster (softAlpha False) is a body that takes the look's depth like vector art,
    and Copy style may take its depth from it."""
    for soft, card in ((True, True), (None, True), (False, False)):
        p = _img_project()
        p.elements[2].softAlpha = soft
        assert stacking.is_card_layer(p.layers[2], stacking.card_elements(p.elements)) is card
        for look in ("clay", "liquid-glass", "neon"):
            st = resolve_look(PRESETS, look)
            out = apply_style(p, st, {"l0": 0.5, "l1": 0.2, "l2": 0.5}, presets=PRESETS)
            d, want = out.layers[2].depth, st.layerDefaults.depth
            if card:
                assert (d.thickness, d.inflate) == (min(want.thickness, 0.02), 0.0), (soft, look)
            else:
                assert (d.thickness, d.bevel, d.inflate) == (want.thickness, want.bevel, want.inflate), (soft, look)
    for soft, want in ((False, 0.4), (True, 0.1)):     # the top-most body gives Copy style its depth
        q = _img_project()
        q.elements[2].softAlpha = soft
        for L, k in zip(q.layers, (0.1, 0.1, 0.4)):
            L.depth.inflate = k
        assert extract_style(q).layerDefaults.depth.inflate == want, soft


def test_apply_style_stacks_overlap_aware():
    """Round 8: with footprints (bis.stacking.LayerShape) a look stacks a layer only above the lower layers it
    overlaps; Copy style's zGap is the clearance of the stacked layers (None when nothing sits on anything)."""
    from shapely.geometry import box

    p = _project(3)
    shapes = {"l0": stacking.LayerShape(0.2, box(-0.8, -0.2, -0.4, 0.2)),     # left
              "l1": stacking.LayerShape(0.2, box(0.4, -0.2, 0.8, 0.2)),       # right: apart from the left
              "l2": stacking.LayerShape(0.1, box(-0.7, -0.1, -0.5, 0.1))}     # on the left one
    style = StyleSpec.model_validate({"layerDefaults": {"depth": {"thickness": 0.16, "bevel": 0.08, "inflate": 0.25}},
                                      "zGap": None})
    out = apply_style(p, style, shapes, presets=PRESETS)
    h0 = 0.16 + 2 * 0.25 * 0.2
    assert [l.depth.z for l in out.layers] == pytest.approx([LIFT, LIFT, LIFT + h0 + GAP])
    assert stacking.stack_gap(out.layers, shapes, presets=PRESETS) == pytest.approx(GAP)
    assert extract_style(out, max_radii=shapes).zGap == pytest.approx(GAP)
    out2 = apply_style(p, style.model_copy(update={"zGap": 0.05}), shapes, presets=PRESETS)
    assert out2.layers[2].depth.z == pytest.approx(LIFT + h0 + 0.05)
    assert extract_style(out2, max_radii=shapes).zGap == pytest.approx(0.05)
    side = {**shapes, "l2": stacking.LayerShape(0.1, box(-0.1, 0.5, 0.1, 0.7))}   # nothing overlaps
    out3 = apply_style(p, style, side, presets=PRESETS)
    assert [l.depth.z for l in out3.layers] == [LIFT] * 3
    assert extract_style(out3, max_radii=side).zGap is None
    # bare radii (footprint unknown) keep the sequential stack - never lower
    seq = apply_style(p, style, {"l0": 0.2, "l1": 0.2, "l2": 0.1}, presets=PRESETS)
    assert [l.depth.z for l in seq.layers] == pytest.approx([LIFT, LIFT + h0 + GAP, LIFT + 2 * (h0 + GAP)])
    # shapes for only some layers (a bundle without one of them): the others take the bbox bound (footprint
    # unknown: they stack on every lower layer) - LayerShapes are kept as they are, never float()-ed
    part = {"l0": shapes["l0"], "l1": shapes["l1"]}
    out4 = apply_style(p, style, part, presets=PRESETS)
    assert [l.depth.z for l in out4.layers][:2] == [LIFT, LIFT]
    assert out4.layers[2].depth.z >= LIFT + h0 + GAP - 1e-9
    assert extract_style(out4, max_radii=part).zGap is not None


# ---------------------------------------------------------------------------------------------- extract
def test_extract_style_roundtrip():
    src = _project(3)
    src.layers[0].material = MaterialSpec(preset="satin")
    for l in src.layers[1:]:
        l.material = MaterialSpec(preset="candy", params={"tint": 0.3})
    src.layers[1].depth = LayerDepth(z=0.15, thickness=0.12, bevel=0.012, bevelSegments=8)   # clamped bevel
    src.layers[2].depth = LayerDepth(z=0.30, thickness=0.12, bevel=0.05, bevelSegments=8, inflate=0.2)
    src.layers[2].mode = "combined"
    src.lighting = Lighting(preset="soft", angle=20)
    src.camera = CameraSpec(zoom=1.1)
    src.render.colorMode = "standard"
    src.canvas.plate.fill = FillSolid(color="#ff00ff")
    s = extract_style(src)
    assert s.layerDefaults.material == MaterialSpec(preset="candy", params={"tint": 0.3})
    # zGap = the median clearance between real body heights: 0.15 - 0.08 and 0.30 - (0.15 + 0.12 + 2 x 0.2 x 0.05)
    assert extract_style(src, max_radii={"l2": 0.05}).zGap == pytest.approx(0.07)
    assert extract_style(src, max_radii={"l0": 0.0, "l1": 0.5, "l2": 0.0}).zGap == pytest.approx(0.07)
    assert s.layerDefaults.depth.bevel == 0.05 and s.layerDefaults.depth.thickness == 0.12  # largest bevel
    assert s.layerDefaults.depth.inflate == 0.2 and s.layerDefaults.depth.z == 0.0
    assert [m.preset for m in s.layerMaterials] == ["satin", "candy", "candy"]
    assert s.zGap == pytest.approx(0.07)                               # the median clearance [0.03, 0.07]
    assert s.plate.fill is None and s.plate.shape is None            # the icon's own colour stays its own
    assert s.lighting.preset == "soft" and s.camera.zoom == 1.1 and s.colorMode == "standard"
    assert extract_style(src, plate_fill=True, plate_shape=True).plate.fill == FillSolid(color="#ff00ff")
    src.canvas.plate.fill = FillSystem(type="system-dark")
    assert extract_style(src).plate.fill == FillSystem(type="system-dark")   # deliberate fills travel

    dst = apply_style(_project(5), s, {"l3": 0.02}, presets=PRESETS)
    assert [l.material.preset for l in dst.layers] == ["satin", "candy", "candy", "candy", "candy"]
    hs = [0.12, 0.12, 0.12, 0.12 + 2 * 0.2 * 0.02, 0.12]
    assert [l.depth.z for l in dst.layers] == pytest.approx([LIFT + sum(hs[:i]) + i * 0.07 for i in range(5)])
    assert all(l.depth.bevel == 0.05 for l in dst.layers)             # no safe-radius clamp (thickness/2 = 0.06)
    assert dst.lighting == src.lighting and dst.render.colorMode == "standard"

    uniform = extract_style(_project(1))
    assert uniform.layerMaterials is None and uniform.zGap is None


def test_clean_params_principled_schema():
    """PLAN 11: params outside the shared Principled schema are dropped; the legacy params that always drove a
    Principled input are renamed; wrong types are dropped and numbers clamped to the slider range."""
    schema = param_schema(PRESETS)
    assert len(schema) == 28 and schema["transmission"]["group"] == "Transmission"
    legacy = {"frost": 0.12, "glow": 0.35, "rim": 1.0, "translucency": 0.75, "dispersion": 0.06, "absorption": 2.0,
              "density": 8, "scatter": 4, "bloom": 0.6, "core": 0.25, "bands": 3, "filmMin": 250, "filmMax": 900,
              "brush": "radial", "specular": "auto", "coat": 1.0, "sheen": 0.15, "subsurface": 1.0,
              "anisotropy": 0.8, "film": 300, "filmIor": 1.6, "strength": 4.0, "tint": 0.5, "grain": 0.08}
    assert clean_params(legacy, schema) == {
        "tint": 0.5, "grain": 0.08, "roughness": 0.12, "coatWeight": 1.0, "sheenWeight": 0.15,
        "subsurfaceWeight": 1.0, "anisotropic": 0.8, "thinFilmThickness": 300, "thinFilmIor": 1.6,
        "emissionStrength": 4.0}
    assert clean_params({"frost": 0.3, "roughness": 0.1}, schema) == {"roughness": 0.1}   # explicit value wins
    assert clean_params({"ior": 0.5, "emissionStrength": 99, "metallic": True, "roughness": "0.2",
                         "paintMode": "emission", "alpha": float("nan"), "__intent": "clear"}, schema) == {
        "ior": 1.0, "emissionStrength": 30, "paintMode": "emission"}
    assert clean_params({"paintMode": "glow"}, schema) == {}
    assert clean_params({"frost": 0.2, "whatever": 1}, {}) == {"frost": 0.2, "whatever": 1}   # no schema: untouched
    for mat in PRESETS["materials"].values():                     # every preset default is valid as an override
        defaults = {k: v["default"] for k, v in mat["params"].items()}
        assert clean_params(defaults, schema) == defaults


def test_styles_entering_the_server_are_cleaned():
    loader = {"src": _project(2)}.__getitem__
    pasted = StyleSpec.model_validate({
        "layerDefaults": {"material": {"preset": "liquid_glass", "params": {"frost": 0.1, "glow": 1.0, "tint": 0.3}},
                          "shadow": {"kind": "neutral", "opacity": 0.4}},
        "layerMaterials": [{"preset": "neon", "params": {"strength": 5, "bloom": 1}}],
        "plate": {"material": {"preset": "satin", "params": {"sheen": 0.2, "translucency": 1}}},
        "camera": {"iso": 0.3, "explode": 2.5},
    })
    s = resolve_style_request(StyleRequest(style=pasted), PRESETS, loader)
    assert s.layerDefaults.material.params == {"tint": 0.3, "roughness": 0.1}
    assert s.layerMaterials[0].params == {"emissionStrength": 5}
    assert s.plate.material.params == {"sheenWeight": 0.2}
    assert s.layerDefaults.shadow.kind == "physical" and s.camera.iso == 0.3 and s.camera.explode == 1.0
    assert pasted.layerDefaults.material.params["glow"] == 1.0                  # the request itself is not mutated
    for look_id in PRESETS["looks"]:                                             # looks are already clean
        look = resolve_look(PRESETS, look_id)
        assert clean_style(look.model_copy(deep=True), PRESETS) == look
        assert look.layerDefaults.shadow.kind in ("physical", "none")


def test_extract_style_ignores_per_shape_materials():
    """Per-shape overrides belong to one icon's element ids - a copied style carries only layer materials."""
    src = _project(2)
    src.layers[1].material = MaterialSpec(preset="frosted_glass", params={"roughness": 0.3})
    src.layers[1].elementMaterials = {"e1": MaterialSpec(preset="chrome")}
    s = extract_style(src)
    assert "elementMaterials" not in json.dumps(s.model_dump(mode="json")) and s.layerMaterials[1].preset == "frosted_glass"
    assert s.layerDefaults.material in (MaterialSpec(), MaterialSpec(preset="frosted_glass", params={"roughness": 0.3}))


def test_resolve_style_request_rules():
    loader = {"src": _project(2)}.__getitem__
    assert resolve_style_request(StyleRequest(look="clay"), PRESETS, loader).layerDefaults.material.preset == "matte_clay"
    # the copied zGap is the clearance between the real body heights: z 0 / 0.2, thickness 0.08
    assert resolve_style_request(StyleRequest(fromProject="src"), PRESETS, loader).zGap == pytest.approx(0.12)
    assert resolve_style_request(StyleRequest(style=StyleSpec(zGap=0.3)), PRESETS, loader).zGap == 0.3
    for bad in (StyleRequest(), StyleRequest(look="clay", fromProject="src")):
        with pytest.raises(StyleError):
            resolve_style_request(bad, PRESETS, loader)
    assert resolve_style_request(StyleRequest(), PRESETS, loader, allow_none=True) is None
    with pytest.raises(LookNotFound):
        resolve_style_request(StyleRequest(look="zzz"), PRESETS, loader)
    seen = []
    custom = resolve_style_request(StyleRequest(fromProject="src"), PRESETS, loader,
                                   extract=lambda pid: seen.append(pid) or StyleSpec(zGap=0.5))
    assert seen == ["src"] and custom.zGap == 0.5


# ---------------------------------------------------------------------------------------------- API
@pytest.fixture
def client(tmp_path):
    app = create_app(make_test_settings(tmp_path, auto_preview=False), bridge=FakeBridge())
    with TestClient(app) as c:
        yield c


def test_looks_endpoint_and_presets(client):
    lk = client.get("/api/looks").json()
    assert set(lk) == set(PRESETS["looks"]) and lk["crystal"]["label"] == "Crystal"
    assert client.get("/api/presets").json()["looks"] == lk


@pytest.mark.skipif(not _real_svg(), reason="bis.svg not importable")
def test_style_endpoints_with_real_geometry(client):
    c = client
    maps = c.post("/api/projects", json={"sample": "Maps"}).json()
    other = c.post("/api/projects", json={"sample": "Find Device"}).json()
    pid, oid = maps["id"], other["id"]

    s = c.get(f"/api/projects/{pid}/style").json()
    assert StyleSpec.model_validate(s).layerDefaults.material.preset == "liquid_glass"
    assert s["plate"]["fill"] is None and s["lighting"]["preset"] == "studio"
    full = c.get(f"/api/projects/{pid}/style?plateFill=true&shape=true").json()["plate"]
    assert full["shape"] == maps["canvas"]["shape"] and full["fill"] == maps["canvas"]["plate"]["fill"]

    with c.websocket_connect("/ws") as ws:
        ws.receive_json()  # system status
        r = c.post(f"/api/projects/{pid}/style", json={"look": "crystal"})
        assert r.status_code == 200, r.text
        end = time.time() + 5
        while time.time() < end:
            ev = ws.receive_json()
            if ev.get("type") == "project":
                assert ev == {"type": "project", "projectId": pid, "event": "saved"}
                break
        else:  # pragma: no cover
            raise AssertionError("no project saved event")
    p = r.json()
    assert c.get(f"/api/projects/{pid}").json() == p                 # saved
    assert p["updatedAt"] > maps["updatedAt"]
    assert all(l["material"]["preset"] == "clear_glass" for l in p["layers"])
    assert p["lighting"]["preset"] == "darkfield" and p["canvas"]["plate"]["fill"] == maps["canvas"]["plate"]["fill"]
    geo = c.get(f"/api/projects/{pid}/geometry").json()
    _assert_real_stack(p, geo, PRESETS["looks"]["crystal"]["style"]["zGap"])
    want = PRESETS["looks"]["crystal"]["style"]["layerDefaults"]["depth"]
    for l in p["layers"]:   # round 7: bevel clamped to thickness/2 only (QA defect 7: no safe-radius clamp)
        assert l["depth"]["bevel"] == pytest.approx(min(want["bevel"], want["thickness"] / 2), abs=1e-5)

    # copy / paste: the style of Maps onto Find Device (by project id and as a pasted StyleSpec)
    r = c.post(f"/api/projects/{oid}/style", json={"fromProject": pid})
    assert r.status_code == 200
    q = r.json()
    assert all(l["material"]["preset"] == "clear_glass" for l in q["layers"]) and q["lighting"]["preset"] == "darkfield"
    pasted = c.get(f"/api/projects/{pid}/style").json()
    pasted["layerDefaults"]["material"] = {"preset": "chrome", "params": {}}
    pasted["layerMaterials"] = None
    r = c.post(f"/api/projects/{oid}/style", json={"style": pasted})
    assert r.status_code == 200 and {l["material"]["preset"] for l in r.json()["layers"]} == {"chrome"}

    # errors
    assert c.post(f"/api/projects/{pid}/style", json={}).status_code == 400
    assert c.post(f"/api/projects/{pid}/style").status_code == 400
    assert c.post(f"/api/projects/{pid}/style", json={"look": "clay", "fromProject": oid}).status_code == 400
    assert c.post(f"/api/projects/{pid}/style", json={"look": "nope"}).status_code == 404
    assert c.post(f"/api/projects/{pid}/style", json={"fromProject": "nope-123456"}).status_code == 404
    assert c.post("/api/projects/nope-123456/style", json={"look": "clay"}).status_code == 404
    assert c.get("/api/projects/nope-123456/style").status_code == 404
    assert c.post(f"/api/projects/{pid}/style", json={"style": {"zGap": "x"}}).status_code == 422


@pytest.mark.skipif(not _real_svg(), reason="bis.svg not importable")
def test_looks_and_copied_styles_keep_tiling_combined_layers(client):
    """QA round 4 #8 (Maps / Gmail): their tiled pieces form ONE 'combined' body; applying any look, or the
    copied style of an icon whose modes are art-derived, keeps it. A mode the user picked does travel."""
    c = client
    maps = c.post("/api/projects", json={"sample": "Maps"}).json()
    gmail = c.post("/api/projects", json={"sample": "Gmail"}).json()
    calc = c.post("/api/projects", json={"sample": "Calculator"}).json()
    assert [l["mode"] for l in maps["layers"]] == ["combined"] and [l["mode"] for l in gmail["layers"]] == ["combined"]
    assert {l["mode"] for l in calc["layers"]} == {"individual"}
    for p in (maps, gmail):
        for look in sorted(PRESETS["looks"]):
            r = c.post(f"/api/projects/{p['id']}/style", json={"look": look})
            assert r.status_code == 200, r.text
            assert [l["mode"] for l in r.json()["layers"]] == ["combined"], (p["name"], look)
        stored = c.get(f"/api/projects/{p['id']}")
        assert stored.status_code == 200 and [l["mode"] for l in stored.json()["layers"]] == ["combined"]
        geo = c.get(f"/api/projects/{p['id']}/geometry").json()
        for l in stored.json()["layers"]:   # round 7: thickness/2 is the only bevel clamp
            assert l["depth"]["bevel"] <= l["depth"]["thickness"] / 2 + 1e-9
            assert geo["layers"][l["id"]]["maxRadius"] > 0

    # copy style: Calculator's modes are its own art's -> not copied; pasting it keeps Maps combined
    s = c.get(f"/api/projects/{calc['id']}/style").json()
    assert s["layerDefaults"]["mode"] is None
    assert [l["mode"] for l in c.post(f"/api/projects/{maps['id']}/style",
                                      json={"fromProject": calc["id"]}).json()["layers"]] == ["combined"]
    assert [l["mode"] for l in c.post(f"/api/projects/{gmail['id']}/style",
                                      json={"style": s}).json()["layers"]] == ["combined"]
    # Maps' own (art-derived) combined mode is not copied onto Calculator's separate symbols either
    assert c.get(f"/api/projects/{maps['id']}/style").json()["layerDefaults"]["mode"] is None
    r = c.post(f"/api/projects/{calc['id']}/style", json={"fromProject": maps["id"]}).json()
    assert {l["mode"] for l in r["layers"]} == {"individual"}
    # Maps split into pieces by its user (or saved before round 4): 'individual' is never copied onto Gmail
    split = c.get(f"/api/projects/{maps['id']}").json()
    split["layers"][0]["mode"] = "individual"
    assert c.put(f"/api/projects/{maps['id']}", json=split).status_code == 200
    assert c.get(f"/api/projects/{maps['id']}/style").json()["layerDefaults"]["mode"] is None
    assert [l["mode"] for l in c.post(f"/api/projects/{gmail['id']}/style",
                                      json={"fromProject": maps["id"]}).json()["layers"]] == ["combined"]

    # a mode the user set on purpose (Calculator fused into combined bodies) is part of the copied look
    edited = c.get(f"/api/projects/{calc['id']}").json()
    for l in edited["layers"]:
        l["mode"] = "combined"
    assert c.put(f"/api/projects/{calc['id']}", json=edited).status_code == 200
    s2 = c.get(f"/api/projects/{calc['id']}/style").json()
    assert s2["layerDefaults"]["mode"] == "combined"
    other = c.post("/api/projects", json={"sample": "Find Device"}).json()
    r = c.post(f"/api/projects/{other['id']}/style", json={"fromProject": calc["id"]}).json()
    assert {l["mode"] for l in r["layers"]} == {"combined"}
    geo = c.get(f"/api/projects/{other['id']}/geometry").json()
    assert all(l["depth"]["bevel"] <= l["depth"]["thickness"] / 2 + 1e-9 for l in r["layers"])
    _assert_real_stack(r, geo, s2["zGap"])          # stacked with the max radii of the COMBINED bodies


def _assert_real_stack(project: dict, geo: dict, gap, tol: float = 2e-4) -> None:
    """The layers form the overlap-aware real-height stack (PLAN 11 rounds 7 + 8) for the bundle's geometry
    (footprints + max radii): z = max(stackLift, max over overlapped lower layers of z + H + gap) with gap = `gap`
    (None = the presets' stackGap), and no two touching layers have overlapping z ranges."""
    from conftest import assert_rule_stack

    assert_rule_stack(project, geo, gap, tol, presets=PRESETS)


@pytest.mark.skipif(not _real_svg(), reason="bis.svg not importable")
@pytest.mark.parametrize("sample", ["Ti84", "Find Device", "Photos"])
def test_every_look_stacks_without_interpenetration(client, sample):
    """QA defect 4: under looks, layers passed through each other (thickness / dome taller than the look's z step).
    Every look now stacks at REAL body heights: the clearance between neighbours is the look's zGap, never < 0."""
    c = client
    src = c.post("/api/projects", json={"sample": sample}).json()
    assert len(src["layers"]) >= 2
    geo0 = c.get(f"/api/projects/{src['id']}/geometry").json()
    _assert_real_stack(src, geo0, None)                                # the import default stack too
    for look in sorted(PRESETS["looks"]):
        r = c.post(f"/api/projects/{src['id']}/style", json={"look": look})
        assert r.status_code == 200, r.text
        p = r.json()
        geo = c.get(f"/api/projects/{src['id']}/geometry").json()
        _assert_real_stack(p, geo, PRESETS["looks"][look]["style"].get("zGap", StyleSpec().zGap))
        depth = PRESETS["looks"][look]["style"]["layerDefaults"]["depth"]
        cards = stacking.card_elements(p["elements"])
        for l in p["layers"]:
            if all(i in cards for i in l["elementIds"]):    # rounds 8 + 9: soft-alpha rasters stay flat cards
                assert l["depth"]["inflate"] == 0.0 and l["depth"]["thickness"] <= 0.02, look
            else:
                assert l["depth"]["bevel"] == pytest.approx(min(depth["bevel"], depth["thickness"] / 2), abs=1e-5)
                assert l["depth"]["inflate"] == depth["inflate"], look


@pytest.mark.skipif(not _real_svg(), reason="bis.svg not importable")
def test_project_saved_with_null_layer_modes_is_repaired(client, tmp_path):
    """The round-4 server wrote ``mode: null`` when a mode-less look was applied: such a project failed to load
    (HTTP 500, and a batch failed every icon). Loading restores each layer's art-derived mode and re-saves."""
    c = client
    maps = c.post("/api/projects", json={"sample": "Maps"}).json()
    calc = c.post("/api/projects", json={"sample": "Calculator"}).json()
    store = c.app.state.store
    for p in (maps, calc):
        path = store.dir(p["id"]) / "project.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        for l in raw["layers"]:
            l["mode"] = None
        path.write_text(json.dumps(raw), encoding="utf-8")
    r = c.get(f"/api/projects/{maps['id']}")
    assert r.status_code == 200 and [l["mode"] for l in r.json()["layers"]] == ["combined"]
    assert r.json()["updatedAt"] == maps["updatedAt"]                 # a quiet repair, not an edit
    assert {l["mode"] for l in c.get(f"/api/projects/{calc['id']}").json()["layers"]} == {"individual"}
    saved = json.loads((store.dir(maps["id"]) / "project.json").read_text(encoding="utf-8"))
    assert [l["mode"] for l in saved["layers"]] == ["combined"]

@pytest.mark.skipif(not _real_svg(), reason="bis.svg not importable")
def test_null_mode_repair_never_overwrites_a_newer_save(client):
    """load() reads project.json without the project lock; its quiet re-save of repaired null modes must not
    clobber a version saved while the modes were being derived (a PUT / style apply landing in that window)."""
    c = client
    maps = c.post("/api/projects", json={"sample": "Maps"}).json()
    store = c.app.state.store
    path = store.dir(maps["id"]) / "project.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["layers"][0]["mode"] = None
    path.write_text(json.dumps(raw), encoding="utf-8")
    real_auto = store.auto_modes

    def auto_with_concurrent_put(project):            # a PUT lands while the auto modes are derived
        newer = dict(maps, name="Renamed meanwhile", updatedAt="2099-01-01T00:00:00.000Z")
        path.write_text(json.dumps(newer), encoding="utf-8")
        return real_auto(project)

    store.auto_modes = auto_with_concurrent_put
    try:
        got = store.load(maps["id"])
    finally:
        store.auto_modes = real_auto
    assert [l.mode for l in got.layers] == ["combined"]                       # the caller still gets a valid project
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["name"] == "Renamed meanwhile" and saved["updatedAt"] == "2099-01-01T00:00:00.000Z"
    # without a concurrent save the repaired copy IS written back (quietly)
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert [l.mode for l in store.load(maps["id"]).layers] == ["combined"]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [l["mode"] for l in saved["layers"]] == ["combined"] and saved["updatedAt"] == raw["updatedAt"]

"""PLAN §11 materials: ONE Principled BSDF per shape, params 1:1, physical rendering (refraction, real shadows).

The first block runs in the backend venv (presets / appearance rules, no Blender); the rest launches REAL Blender
5.0 (OptiX). GPU rules: renders ≤ 256 px, ≤ 32 spp.

    .venv/Scripts/python.exe -m pytest -q tests/blender/test_materials.py
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

import test_worker_quality as T  # noqa: E402  (synthetic scene builders)
import worker_client as wc  # noqa: E402
from blender_worker import appearance as A  # noqa: E402
from blender_worker import presets as P  # noqa: E402
from blender_worker.defaults import norm_project  # noqa: E402
from blender_worker.util import BRAND_KNEE, soft_clip  # noqa: E402

needs_blender = pytest.mark.skipif(not wc.blender_available(), reason="Blender 5.0 not installed")
PRESETS = P.material_ids()
FORBIDDEN = {"ShaderNodeMixShader", "ShaderNodeAddShader", "ShaderNodeEmission", "ShaderNodeBsdfTransparent",
             "ShaderNodeBsdfGlass", "ShaderNodeBsdfRefraction", "ShaderNodeVolumeAbsorption",
             "ShaderNodeVolumeScatter", "ShaderNodeVolumePrincipled", "ShaderNodeLightPath", "ShaderNodeGroup",
             "ShaderNodeBsdfDiffuse", "ShaderNodeBsdfGlossy", "ShaderNodeBsdfTranslucent", "ShaderNodeHoldout",
             "ShaderNodeAttribute", "ShaderNodeObjectInfo", "ShaderNodeLayerWeight", "ShaderNodeFresnel"}
MAX_NODES = 12          # a typical shape graph (texture paint + every common pre-processing helper)
PX, SPP = 128, 24
# Principled inputs fed 1:1 by a param (materials.PRINCIPLED + Alpha / Thin Film Thickness)
ONE_TO_ONE = {"metallic": "Metallic", "roughness": "Roughness", "ior": "IOR", "diffuseRoughness": "Diffuse Roughness",
              "subsurfaceWeight": "Subsurface Weight", "subsurfaceScale": "Subsurface Scale",
              "subsurfaceAnisotropy": "Subsurface Anisotropy", "specularIorLevel": "Specular IOR Level",
              "anisotropic": "Anisotropic", "anisotropicRotation": "Anisotropic Rotation",
              "transmission": "Transmission Weight", "coatWeight": "Coat Weight", "coatRoughness": "Coat Roughness",
              "coatIor": "Coat IOR", "sheenWeight": "Sheen Weight", "sheenRoughness": "Sheen Roughness",
              "emissionStrength": "Emission Strength", "thinFilmIor": "Thin Film IOR",
              "thinFilmThickness": "Thin Film Thickness", "alpha": "Alpha"}
PRE = {"paintMode", "tint", "grain", "grainScale", "filmVariation", "specularTint", "coatTint", "sheenTint"}


# ------------------------------------------------------------------------------------------------ venv only
def test_every_preset_is_the_one_principled_schema():
    """28 params, grouped like Blender's Principled panel; presets are only starting values of the same schema."""
    data = P.load()
    groups = data["principledSchema"]["groups"]
    keys = None
    for pid, m in data["materials"].items():
        params = m["params"]
        if keys is None:
            keys = list(params)
        assert list(params) == keys, pid
        for k, spec in params.items():
            assert spec.get("group") in groups, (pid, k)
            if spec["type"] == "number":
                assert spec["min"] <= spec["default"] <= spec["max"], (pid, k)
    assert len(keys) == 28
    assert set(keys) == set(ONE_TO_ONE) | PRE                    # every param drives the graph


def test_shape_material_merges_layer_and_element_overrides():
    lm = {"preset": "satin", "params": {"roughness": 0.3, "tint": 0.5}}
    preset, params = P.resolve_material(lm)
    assert preset == "satin" and params["roughness"] == 0.3 and params["coatWeight"] == P.material_params("satin")["coatWeight"]
    preset, params = P.resolve_material(lm, {"preset": "satin", "params": {"roughness": 0.8}})
    assert preset == "satin" and params["roughness"] == 0.8 and params["tint"] == 0.5     # layer params kept
    preset, params = P.resolve_material(lm, {"preset": "chrome", "params": {"roughness": 0.1}})
    assert preset == "chrome" and params["roughness"] == 0.1                              # another preset starts fresh
    assert params["tint"] == P.material_params("chrome")["tint"] and params["metallic"] == 1.0
    assert P.resolve_material({"preset": "bogus"})[0] == "liquid_glass"
    # legacy names of saved projects map onto the Principled schema; values are clamped to the slider range
    params = P.material_params("frosted_glass", {"frost": 0.4, "coat": 0.7, "ior": 9.0})
    assert params["roughness"] == 0.4 and params["coatWeight"] == 0.7 and params["ior"] == 3.0


def _proj(layers, plate_preset="satin"):
    return norm_project({"id": "p", "layers": layers, "canvas": {"plate": {"material": {"preset": plate_preset}}},
                         "appearances": {"tint": {"color": "#ff0000", "strength": 1.0}}})


def test_appearances_only_change_principled_inputs():
    layers = [{"id": "A", "material": {"preset": "chrome", "params": {"roughness": 0.3}},
               "elementMaterials": {"e": {"preset": "neon"}}},
              {"id": "B", "visible": False, "material": {"preset": "satin"}}]
    bundle = {"layers": {"A": {"regions": [{"paint": {"type": "solid", "color": "#336699"}}]}}}
    light = A.resolve(_proj(layers), "light", bundle)
    assert light["project"]["layers"][0]["material"]["preset"] == "chrome" and light["env"]["mono"] is None
    dark = A.resolve(_proj(layers), "dark", bundle)
    assert dark["env"]["envScale"] < 1 and dark["project"]["canvas"]["plate"]["fill"]["type"] == "system-dark"
    for ap in ("clear-light", "clear-dark"):
        r = A.resolve(_proj(layers), ap, bundle)
        L = r["project"]["layers"][0]
        m = P.material_params(L["material"]["preset"], L["material"]["params"])
        assert m["tint"] == 0.0 and m["transmission"] == 1.0 and 0.15 <= m["roughness"] <= 0.35      # clear glass
        assert L["elementMaterials"] == {} and r["env"]["mono"] is None
        assert r["project"]["layers"][1]["material"]["preset"] == "satin"                         # hidden: untouched
        plate = r["project"]["canvas"]["plate"]
        assert P.material_params(plate["material"]["preset"], plate["material"]["params"])["transmission"] == 1.0
        assert r["env"]["wallpaper"] == ("dark" if ap.endswith("dark") else "light")
    for ap in ("tinted-light", "tinted-dark"):
        r = A.resolve(_proj(layers), ap, bundle)
        mono = r["env"]["mono"]
        assert mono["tint"][0] > 0.9 and mono["tint"][1] < 0.1           # base = mono luminance x tint colour
        assert mono["hi"] == pytest.approx(0.1251, abs=1e-3)                 # #336699: the brightest paint ...
        assert mono["lo"] == pytest.approx(mono["hi"] - A.MONO_MIN_RANGE)    # ... keeps the full tint
        m = P.material_params("liquid_glass", r["project"]["layers"][0]["material"]["params"])
        assert m["tint"] == 1.0
    assert A.resolve(_proj(layers), "tinted-dark", bundle)["project"]["canvas"]["plate"]["fill"]["type"] == "system-dark"


def test_dark_renditions_keep_glyphs_lit():
    """QA r8 #2: clear glass over the near-black dark plate only transmits that plate; glyphs vanished. Dark and
    tinted-dark cap transmission (DARK_GLYPH) and let the art scatter (subsurface) as the base colour: Principled
    inputs only, no emission; opaque shapes and a layer whose dark material the user set are left alone."""
    layers = [{"id": "A", "material": {"preset": "liquid_glass", "params": {"roughness": 0.1}},
               "elementMaterials": {"e": {"preset": "clear_glass"}, "m": {"preset": "chrome"}}},
              {"id": "B", "material": {"preset": "satin"}},
              {"id": "C", "material": {"preset": "clear_glass"}}]
    proj = norm_project({"id": "p", "layers": layers, "appearances": {"dark": {"layers": {
        "C": {"material": {"preset": "clear_glass", "params": {"transmission": 1.0}}}}}}})
    bundle = {"layers": {"A": {"regions": [{"paint": {"type": "solid", "color": "#336699"}}]}}}
    dark = A.resolve(proj, "dark", bundle)["project"]["layers"]
    la = P.material_params(dark[0]["material"]["preset"], dark[0]["material"]["params"])
    assert la["transmission"] == A.DARK_GLYPH["transmission"] and la["subsurfaceWeight"] >= 1.0
    assert la["tint"] >= A.DARK_GLYPH["tintMin"] and la["roughness"] == pytest.approx(0.1)   # the rest is kept
    ems = dark[0]["elementMaterials"]
    e = P.resolve_material(dark[0]["material"], ems["e"])[1]
    assert ems["e"]["preset"] == "clear_glass" and e["transmission"] == A.DARK_GLYPH["transmission"]
    assert ems["m"] == {"preset": "chrome", "params": {}}                                      # opaque: untouched
    assert P.material_params("satin", dark[1]["material"]["params"])["transmission"] == 0.0
    assert P.material_params("clear_glass", dark[2]["material"]["params"])["transmission"] == 1.0   # explicit
    light = A.resolve(proj, "light", bundle)["project"]["layers"]
    assert P.material_params("liquid_glass", light[0]["material"]["params"])["transmission"] == 1.0
    td = A.resolve(proj, "tinted-dark", bundle)
    m = P.material_params("liquid_glass", td["project"]["layers"][0]["material"]["params"])
    assert m["transmission"] == A.DARK_GLYPH["transmission"] and m["subsurfaceWeight"] >= 1.0
    assert m["emissionStrength"] == 0.0 and m["paintMode"] == "base"                    # lit, not self-lit
    assert td["env"]["mono"]["floor"] == A.MONO_FLOOR_DARK
    assert A.resolve(proj, "tinted-light", bundle)["env"]["mono"]["floor"] == A.MONO_FLOOR


def test_soft_clip_is_identity_below_the_knee_and_rolls_off_above():
    assert soft_clip((0.0, 0.5, BRAND_KNEE)) == (0.0, 0.5, BRAND_KNEE)
    hi = soft_clip((1.0, 1.3, 1.6))
    assert BRAND_KNEE < hi[0] < hi[1] < hi[2] < 1.0                 # monotone, never reaches 1 (no hard clip)
    eps = 1e-4                                                       # C1 at the knee
    assert abs((soft_clip((BRAND_KNEE + eps,))[0] - BRAND_KNEE) / eps - 1.0) < 1e-2


def test_brand_colour_mode_is_the_worker_default():
    cm = P.load()["colorModes"]["brand"]
    assert cm["viewTransform"] == "Standard" and cm["look"] == "None"
    assert cm["softClip"] == pytest.approx(BRAND_KNEE)
    assert P.DEFAULT_COLOR_MODE == "brand"
    assert P.color_mode_id(None) == "brand" and P.color_mode_id("bogus") == "brand"
    assert P.color_mode_id("neutral") == "neutral" and P.soft_clip_knee("neutral") == 0.0


# ------------------------------------------------------------------------------------------------ Blender
@pytest.fixture(scope="module")
def worker():
    if not wc.blender_available():
        pytest.skip("Blender 5.0 not installed")
    w = wc.Worker(warmup="basic")
    yield w
    w.close()


@pytest.fixture(scope="module")
def outdir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("bis_materials")


@pytest.fixture(scope="module")
def photos() -> dict:
    import make_fixtures
    index_path = HERE / "_fixtures" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    if "Photos" not in index or not Path(index["Photos"]["geometryPath"]).exists():
        index = make_fixtures.make(["Photos"], HERE / "_fixtures")
    e = index["Photos"]
    return {"project": json.loads(Path(e["project"]).read_text(encoding="utf-8")), "geometryPath": e["geometryPath"]}


def render(w, proj_bundle, out, quality="draft", size=PX, **kw):
    proj, bundle = proj_bundle
    args = {"project": proj, "geometry": bundle, "quality": quality, "size": size, "out": str(out)}
    if quality == "preview":
        args["samples"] = SPP
    args.update(kw)
    return w.result("render", args)


def two_discs(params=None, elem=None, preset="satin", shadow="physical", z=0.0):
    """One layer 'Discs' with two regions e1 (left) / e2 (right), solid paints."""
    lay = T.layer("A", z=z, preset=preset, params=params)
    lay["name"] = "Discs"
    lay["shadow"] = {"kind": shadow, "opacity": 0.5}
    if elem is not None:
        lay["elementMaterials"] = elem
    g = T.geo([("e1", [T.circle(0.3, -0.45)], "#3366ff", 1.0), ("e2", [T.circle(0.3, 0.45)], "#ff6633", 1.0)])
    return T.scene([lay], {"A": g}, color_mode="brand")


def check_graph(name: str, m: dict) -> None:
    types = m["types"]
    assert types.count("ShaderNodeBsdfPrincipled") == 1, (name, types)
    assert types.count("ShaderNodeOutputMaterial") == 1, (name, types)
    assert not FORBIDDEN & set(types), (name, sorted(FORBIDDEN & set(types)))


@needs_blender
def test_every_preset_and_shape_is_one_principled(worker, photos, outdir):
    """Every preset, every shape of a real icon: pre-processing → ONE Principled BSDF → Material Output, one material
    per shape object named 'BIS <layer name> / <element id>', ≤ ~12 nodes."""
    for preset in PRESETS:
        fx = copy.deepcopy(photos)
        for L in fx["project"]["layers"]:
            L["material"] = {"preset": preset, "params": {}}
        r = worker.result("render", {"project": fx["project"], "geometryPath": fx["geometryPath"], "quality": "draft",
                                     "size": 64, "out": str(outdir / f"one_{preset}.png")})
        info = worker.result("scene_info")
        mats = info["materials"]
        shapes = [o for o in info["objects"] if o["type"] == "MESH" and o["name"] not in ("BIS Plate", "BIS Wallpaper")]
        assert len(shapes) == r["stats"]["pieces"]
        used = {o["material"] for o in shapes}
        assert len(used) == len(shapes), "one material per shape object"
        names = {L["name"] for L in fx["project"]["layers"]}
        for o in shapes:
            assert any(o["material"].startswith(f"BIS {n} / ") for n in names), o["material"]
        for name, m in mats.items():
            check_graph(name, m)
            assert m["preset"] in (preset, "satin"), (name, m["preset"])
            if m["preset"] == preset:
                assert m["nodes"] <= MAX_NODES, (preset, name, m["nodes"], m["types"])


@needs_blender
def test_params_map_one_to_one_onto_principled_inputs(worker, outdir):
    vals = {"metallic": 0.11, "roughness": 0.22, "ior": 1.33, "diffuseRoughness": 0.44, "subsurfaceWeight": 0.55,
            "subsurfaceScale": 0.066, "subsurfaceAnisotropy": 0.77, "specularIorLevel": 0.88, "anisotropic": 0.12,
            "anisotropicRotation": 0.23, "transmission": 0.34, "coatWeight": 0.45, "coatRoughness": 0.56,
            "coatIor": 1.67, "sheenWeight": 0.78, "sheenRoughness": 0.89, "emissionStrength": 2.5,
            "thinFilmThickness": 345.0, "thinFilmIor": 1.23, "alpha": 0.9}
    render(worker, two_discs(vals), outdir / "one_to_one.png")
    m = worker.result("scene_info")["materials"]["BIS Discs / e1"]
    for key, sock in ONE_TO_ONE.items():
        assert m["principled"][sock] == pytest.approx(vals[key], abs=1e-4), (key, sock)
    # pre-processing: art -> Base Color / Subsurface Radius; Tangent for anisotropy; plain values otherwise
    assert {"Base Color", "Subsurface Radius", "Tangent"} <= set(m["linked"])
    assert not {"Normal", "Thin Film Thickness", "Specular Tint", "Coat Tint", "Sheen Tint", "Emission Color"} & set(m["linked"])
    # every pre-processing helper switched on: still one Principled, the extra inputs linked
    render(worker, two_discs({**vals, "grain": 0.3, "filmVariation": 0.5, "specularTint": 0.5, "coatTint": 0.5,
                              "sheenTint": 0.5, "paintMode": "base+emission"}), outdir / "one_to_one_pre.png")
    m = worker.result("scene_info")["materials"]["BIS Discs / e1"]
    check_graph("e1", m)
    assert {"Normal", "Thin Film Thickness", "Specular Tint", "Coat Tint", "Sheen Tint", "Emission Color"} <= set(m["linked"])
    assert "ShaderNodeBump" in m["types"] and m["types"].count("ShaderNodeTexNoise") == 2


@needs_blender
def test_element_materials_override_one_shape(worker, outdir):
    render(worker, two_discs({"roughness": 0.3}, {"e2": {"preset": "satin", "params": {"roughness": 0.77}}}),
           outdir / "elem0.png")
    mats = worker.result("scene_info")["materials"]
    assert mats["BIS Discs / e1"]["principled"]["Roughness"] == pytest.approx(0.3)
    assert mats["BIS Discs / e2"]["principled"]["Roughness"] == pytest.approx(0.77)
    assert mats["BIS Discs / e2"]["principled"]["Coat Weight"] == pytest.approx(P.material_params("satin")["coatWeight"])
    render(worker, two_discs({"roughness": 0.3}, {"e2": {"preset": "chrome", "params": {}}}), outdir / "elem1.png")
    mats = worker.result("scene_info")["materials"]
    assert mats["BIS Discs / e2"]["preset"] == "chrome" and mats["BIS Discs / e2"]["principled"]["Metallic"] == 1.0
    assert mats["BIS Discs / e1"]["preset"] == "satin" and mats["BIS Discs / e1"]["principled"]["Metallic"] == 0.0


@needs_blender
def test_element_material_splits_an_auto_merged_layer(worker, outdir):
    """Touching opaque pieces render as ONE body (scene.touching_opaque), unless a shape has its own material:
    the override must not be dropped silently, so that layer renders its pieces individually."""
    lay = T.layer("A", preset="satin")
    g = T.geo([("e1", [T.rect(-0.5, -0.3, 0.0, 0.3)], "#3366ff", 1.0),
               ("e2", [T.rect(0.0, -0.3, 0.5, 0.3)], "#ff6633", 1.0)])
    g["silhouette"] = [T.rect(-0.5, -0.3, 0.5, 0.3)]          # the union: one outline for two regions
    render(worker, T.scene([lay], {"A": g}), outdir / "merged.png")
    objs = {o["name"]: o for o in worker.result("scene_info")["objects"]}
    assert "BIS A sil" in objs and "BIS A r0" not in objs
    lay["elementMaterials"] = {"e2": {"preset": "chrome", "params": {}}}
    render(worker, T.scene([lay], {"A": g}), outdir / "split.png")
    info = worker.result("scene_info")
    objs = {o["name"]: o for o in info["objects"]}
    assert "BIS A sil" not in objs and {"BIS A r0", "BIS A r1"} <= set(objs)
    assert info["materials"][objs["BIS A r1"]["material"]]["principled"]["Metallic"] == 1.0
    assert info["materials"][objs["BIS A r0"]["material"]]["principled"]["Metallic"] == 0.0


@needs_blender
def test_value_changes_update_in_place(worker, outdir):
    """Sliders and presets with the same graph shape only change values (no EEVEE recompile); switching on a
    pre-processing helper (grain, anisotropy) rebuilds the graph."""
    render(worker, two_discs({"roughness": 0.3}), outdir / "inplace0.png")
    before = worker.result("scene_info")["materials"]["BIS Discs / e1"]
    render(worker, two_discs({"roughness": 0.6, "tint": 0.4, "coatWeight": 0.9}), outdir / "inplace1.png")
    after = worker.result("scene_info")["materials"]["BIS Discs / e1"]
    assert after["nodeIds"] == before["nodeIds"] and after["principled"]["Roughness"] == pytest.approx(0.6)
    render(worker, two_discs(preset="glossy_plastic"), outdir / "inplace2.png")
    swapped = worker.result("scene_info")["materials"]["BIS Discs / e1"]
    assert swapped["preset"] == "glossy_plastic" and swapped["nodeIds"] == before["nodeIds"]
    render(worker, two_discs(preset="brushed_metal"), outdir / "inplace3.png")
    rebuilt = worker.result("scene_info")["materials"]["BIS Discs / e1"]
    assert rebuilt["key"] != before["key"] and {"Normal", "Tangent"} <= set(rebuilt["linked"])


@needs_blender
def test_eevee_material_settings(worker, outdir):
    """EEVEE draws the same graph: raytraced refraction through a sphere of the piece's thickness on transmissive
    shapes, alpha-BLENDED translucent ones (only their front-most surface); the scene carries the plate's sphere
    probe, which sees the plate only (every layer body hidden from it); Cycles ignores both."""
    render(worker, two_discs(preset="clear_glass", elem={"e2": {"preset": "satin", "params": {"alpha": 0.5}}}),
           outdir / "eevee.png")
    info = worker.result("scene_info")
    mats = info["materials"]
    e1, e2 = mats["BIS Discs / e1"], mats["BIS Discs / e2"]
    assert e1["raytraceRefraction"] is True and e1["renderMethod"] == "DITHERED" and e1["thicknessMode"] == "SPHERE"
    assert e2["raytraceRefraction"] is False and e2["renderMethod"] == "BLENDED" and e2["thicknessMode"] == "SLAB"
    assert e2["transparencyOverlap"] is False
    # translucent GLASS is blended too (dithered alpha never converged: speckle) and reads the plate probe
    render(worker, two_discs(preset="clear_glass", elem={"e2": {"preset": "clear_glass", "params": {"alpha": 0.6}}}),
           outdir / "eevee2.png")
    info = worker.result("scene_info")
    e2 = info["materials"]["BIS Discs / e2"]
    assert e2["renderMethod"] == "BLENDED" and e2["raytraceRefraction"] is False
    objs = {o["name"]: o for o in info["objects"]}
    assert objs["BIS Probe"]["type"] == "LIGHT_PROBE"
    assert objs["BIS Plate"]["hideProbeSphere"] is False
    assert objs["BIS A r0"]["hideProbeSphere"] is True and objs["BIS A r1"]["hideProbeSphere"] is True


def translucent_disc(z=0.0, opacity=0.66, colour="#ffffff", plate="#ff7c3b", r=0.3):
    lay = T.layer("A", z=z, preset="liquid_glass", bevel=0.08, thickness=0.16)
    lay["depth"]["inflate"] = 0.25
    g = T.geo([("e", [T.circle(r)], colour, opacity)], safe=r)
    return T.scene([lay], {"A": g}, plate_fill=plate, color_mode="brand")


@needs_blender
def test_translucent_glass_drafts_track_cycles(worker, outdir):
    """QA r9 N1: a 66 % white liquid-glass head on an orange plate (Contacts) rendered near-black in EEVEE drafts
    ((104,70,67) against Cycles' (207,93,67)): alpha-blended glass cannot trace and read the dark studio world. With the
    plate probe the draft head reads the plate through the glass like the Cycles preview."""
    got = {}
    for q in ("draft", "preview"):
        out = outdir / f"translucent_{q}.png"
        render(worker, translucent_disc(), out, quality=q)
        got[q] = rgb_at(out, 0.0, 0.0, r=5)
    d, pv = got["draft"], got["preview"]
    assert d.sum() > 0.8 * pv.sum(), got                               # not dark (HEAD: 0.6x)
    assert d[0] > d[1] + 50 and d[0] > 150, got                          # the orange plate seen through the glass


@needs_blender
def test_floating_glass_draft_at_iso_is_not_dark(worker, outdir):
    """QA r9 N2: glass floating above the plate in the CAD iso view refracted the dark studio world in drafts (Photos'
    petals (76,65,50) against Cycles' (155,126,85)): the plate probe gives the screen-space refraction a plate to fall
    back to. Compared over the disc's pixels in the Cycles render."""
    imgs = {}
    for q in ("draft", "preview"):
        out = outdir / f"floating_{q}.png"
        render(worker, translucent_disc(z=0.45, opacity=1.0, colour="#f4b400", plate="#ffffff", r=0.25), out,
               quality=q, camera={"view": "front", "iso": 1.0})
        imgs[q] = T.rgba(out)[..., :3]
    pv = imgs["preview"]
    mask = (pv[..., 0] - pv[..., 2] > 45) & (pv.sum(-1) > 150)           # the yellow glass in the Cycles render
    assert mask.sum() > 200, mask.sum()
    d, p = imgs["draft"][mask].mean(0), pv[mask].mean(0)
    assert d.sum() > 0.8 * p.sum(), (d.round(), p.round())               # HEAD: about half as bright


def _black_pixels(path) -> int:
    a = T.rgba(path)
    return int(((a[..., :3].sum(-1) < 30) & (a[..., 3] > 200)).sum())


@needs_blender
@pytest.mark.parametrize("art_scale", [0.9, 1.0, 1.073])
def test_flat_blended_glass_is_never_black(worker, outdir, art_scale):
    """Round-8 review: an orthographic head-on view EXACTLY parallel to a flat face's normal made EEVEE's forward
    (alpha-blended) refraction NaN: a flat 60 % glass disc (inflate 0, Contacts' head as a thin card) and Find Device's
    flat soft-alpha shine card rendered as pure black shapes in drafts at art scales 0.8-1.1 (1.072957 happened to
    round clear). The head-on camera is pitched by scene.FRONT_TILT (0.01°)."""
    lay = T.layer("A", preset="liquid_glass", bevel=0.006, thickness=0.02)
    g = T.geo([("e1", [T.circle(0.3, -0.45)], "#ffffff", 0.6), ("e2", [T.circle(0.3, 0.45)], "#ffffff", 0.6)])
    proj, bundle = T.scene([lay], {"A": g}, plate_fill="#ff7c3b", color_mode="brand")
    proj["canvas"]["art"] = {"x": 0.0, "y": 0.0, "scale": art_scale}
    out = outdir / f"flat_blended_{art_scale}.png"
    render(worker, (proj, bundle), out)
    mats = worker.result("scene_info")["materials"]
    assert {m["renderMethod"] for k, m in mats.items() if k.startswith("BIS A /")} == {"BLENDED"}
    assert _black_pixels(out) == 0, _black_pixels(out)                    # before: both discs (1571-2223 px)
    c = rgb_at(out, -0.45 * art_scale, 0.0)
    assert c[0] > 150 and c[0] > c[2] + 60, c.round()                     # the orange plate through the glass


def raster_cards(outdir: Path):
    """Two flat raster glass cards (liquid glass, inflate 0): e1's PNG is opaque inside its disc (its only partial alpha
    the anti-aliased rim: iMessage / Feit / Outlook), e2's a soft radial glow (Find Device's shine)."""
    import numpy as np
    from PIL import Image
    n = 128
    yy, xx = np.mgrid[0:n, 0:n]
    r = np.hypot(xx - (n - 1) / 2, yy - (n - 1) / 2) / (n / 2)
    rgb = np.zeros((n, n, 3), np.uint8)
    rgb[...] = (40, 90, 200)
    paths = {}
    for k, a in (("e1", np.clip((1.0 - r) * 40.0, 0, 1)), ("e2", np.clip(1.0 - r, 0, 1) * 0.6)):
        p = outdir / f"raster_{k}.png"
        Image.fromarray(np.dstack([rgb, (a * 255).astype(np.uint8)]), "RGBA").save(p)
        paths[k] = str(p)
    lay = T.layer("A", preset="liquid_glass", bevel=0.006, thickness=0.02)
    lay["name"] = "Raster"
    g = T.geo([("e1", [T.circle(0.4, -0.45)], "#2850c8", 1.0), ("e2", [T.circle(0.4, 0.45)], "#2850c8", 1.0)])
    g["images"] = [{"elementId": "e1", "path": paths["e1"], "bbox": [-0.85, -0.4, -0.05, 0.4]},
                   {"elementId": "e2", "path": paths["e2"], "bbox": [0.05, -0.4, 0.85, 0.4]}]
    return T.scene([lay], {"A": g}, plate_fill="#ffffff", color_mode="brand")


@needs_blender
def test_raster_glass_dithers_unless_its_alpha_is_soft(worker, outdir):
    """Round-8 review: glass whose art alpha is only a coverage mask stays DITHERED + raytraced; only SOFT art alpha
    (≥ materials.SOFT_ALPHA of the covered pixels partly transparent: a glow / shine) is alpha-blended. Blended glass
    refracts the plate probe, a single-sample capture that carries the bodies' noisy shadows; iMessage's, Feit's and
    Outlook's flat raster glass turned blotchy."""
    render(worker, raster_cards(outdir), outdir / "raster_cards.png")
    mats = worker.result("scene_info")["materials"]
    e1, e2 = mats["BIS Raster / e1"], mats["BIS Raster / e2"]
    assert e1["renderMethod"] == "DITHERED" and e1["raytraceRefraction"] is True, e1
    assert e2["renderMethod"] == "BLENDED" and e2["raytraceRefraction"] is False, e2
    assert _black_pixels(outdir / "raster_cards.png") == 0


@needs_blender
def test_crisp_raster_is_a_height_field_body(worker, outdir):
    """PLAN §11 round 9: a raster with a crisp alpha silhouette (iMessage's bubble, Feit's house, Outlook) is a real
    body like vector art: the server gives its layer the default depth; the worker builds the height field over the
    traced outline and paints it with the PNG (Base Color art). Draft and preview both show the art through the glass
    (never black), the dome is as tall as a vector body's, and the soft glow beside it stays a blended flat card."""
    proj, bundle = raster_cards(outdir)
    L = proj["layers"][0]
    L["depth"].update({"thickness": 0.16, "bevel": 0.08, "inflate": 0.25})
    got = {}
    for q in ("draft", "preview"):
        out = outdir / f"raster_body_{q}.png"
        render(worker, (proj, bundle), out, quality=q, size=PX)
        got[q] = rgb_at(out, -0.45, 0.0, r=5)
        if q == "draft":
            info = worker.result("scene_info", {"check": True})
            objs = {o["name"]: o for o in info["objects"]}
            body = objs["BIS A r0"]
            assert body["route"] == "heightfield" and body["check"]["nonManifold"] == 0, body
            assert body["location"][2] > 0.08, body          # mid-plane of a 0.16 body (a flat card's: 0.012)
            mats = info["materials"]
            e1 = mats["BIS Raster / e1"]
            assert "ShaderNodeTexImage" in e1["types"] and "Base Color" in e1["linked"], e1
            assert e1["renderMethod"] == "DITHERED" and e1["raytraceRefraction"] is True, e1
            assert mats["BIS Raster / e2"]["renderMethod"] == "BLENDED"
            assert _black_pixels(out) == 0
    d, p = got["draft"], got["preview"]
    for c in (d, p):
        assert c[2] > c[0] + 25 and c.sum() > 250, got                   # the blue art through clear glass
    assert abs(d.sum() - p.sum()) < 0.25 * p.sum(), got


@needs_blender
def test_flat_raster_glass_draft_is_smooth(worker, outdir):
    """iMessage's bubble (an opaque raster, a flat image card) in a 256 px draft: its high-pass grain inside the bubble
    stays at the Cycles preview's level; blended, it was a blotchy pattern from the plate probe (1.5 vs 0.5)."""
    import make_fixtures
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view
    index = make_fixtures.make(["iMessage"], HERE / "_fixtures")
    e = index["iMessage"]
    proj = json.loads(Path(e["project"]).read_text(encoding="utf-8"))
    out = outdir / "imessage_draft.png"
    worker.result("render", {"project": proj, "geometryPath": e["geometryPath"], "quality": "draft", "size": 256,
                             "out": str(out)})
    a = T.rgba(out)[..., :3].mean(-1)
    k = 5
    hp = a - sliding_window_view(np.pad(a, k // 2, mode="edge"), (k, k)).mean(axis=(-1, -2))
    grain = float(T.patch(hp[..., None], 0.0, 0.08, 24).std())
    assert grain < 1.0, grain


@needs_blender
@pytest.mark.parametrize("appearance", ["clear-light", "clear-dark"])
def test_glass_plate_probe_sees_the_wallpaper(worker, outdir, appearance):
    """Round-8 review: in the clear (and tinted-light) renditions the plate is frosted glass over the wallpaper; the
    plate probe captured the glass plate itself and EEVEE drafts showed a flat grey plate ((109,109,109) where Cycles
    shows the lavender wallpaper (212,221,248); clear-dark (92,92,92) vs navy (63,73,112)). The plate's probe never sees
    a glass plate: over a light wallpaper the plate is hidden from it; over a dark one (round 9) it sits inside the plate
    and clips past the plate's faces, while the glyph probe above captures the plate for the glyphs (scene._probe)."""
    lay = T.layer("A", preset="liquid_glass", bevel=0.08, thickness=0.16)
    scn = T.scene([lay], {"A": T.geo([("e", [T.circle(0.3)], "#ffffff", 1.0)])}, color_mode="brand")
    got = {}
    for q in ("draft", "preview"):
        out = outdir / f"glass_plate_{appearance}_{q}.png"
        render(worker, scn, out, quality=q, appearance=appearance)
        got[q] = rgb_at(out, -0.7, 0.55)
        if q == "draft":
            info = worker.result("scene_info")
            objs = {o["name"]: o for o in info["objects"]}
            probes = info["probes"]
            assert info["materials"]["BIS Plate"]["raytraceRefraction"] is False      # drawn in EEVEE's opaque layer
            if appearance == "clear-light":
                assert objs["BIS Plate"]["hideProbeSphere"] is True and "BIS Glyph Probe" not in probes
                assert objs["BIS A r0"]["hideProbeSphere"] is True
                assert info["eevee"]["traceMaxRoughness"] == pytest.approx(0.3)       # frosted glyphs trace the plate
            else:
                assert objs["BIS Plate"]["hideProbeSphere"] is False and objs["BIS A r0"]["hideProbeSphere"] is False
                pp, gp = probes["BIS Probe"], probes["BIS Glyph Probe"]
                assert -0.3 < pp["location"][2] < 0.0 and pp["clipStart"] > -pp["location"][2] + 0.2, pp   # mid-plate
                assert gp["influence"] == "BOX" and gp["location"][2] - gp["scale"][2] == pytest.approx(0.03, abs=1e-4)
                assert gp["location"][2] + gp["scale"][2] > objs["BIS A r0"]["location"][2] + 0.08, gp   # holds it
                assert info["eevee"]["traceMaxRoughness"] == pytest.approx(0.2)
    d, p = got["draft"], got["preview"]
    assert d[2] > d[0] + 8 and p[2] > p[0] + 8, (d.round(), p.round())   # the wallpaper's blue, not a neutral grey
    assert abs(d.sum() - p.sum()) < 0.4 * p.sum(), (d.round(), p.round())   # before: 0.52 (clear-light)


def _lstar(rgb) -> float:
    c = np.asarray(rgb, dtype=float) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    y = float(lin @ np.array([0.2126, 0.7152, 0.0722]))
    return 116.0 * (y ** (1 / 3) if y > 0.008856 else 7.787 * y + 16 / 116) - 16.0


@needs_blender
@pytest.mark.parametrize("appearance", ["clear-light", "tinted-light", "clear-dark"])
def test_glass_plate_without_layers_keeps_its_probe(worker, outdir, appearance):
    """QA r11 N11: an icon with no visible layer (Template; every layer hidden) lost the plate probe, so its frosted
    glass plate refracted the dark studio world in drafts (Template clear-light L* 35 vs Cycles' 91, tinted-light the
    same). The plate probe exists whenever the plate is glass (no glyph probe without bodies); the draft plate stays
    within a few L* of the Cycles preview."""
    hidden = T.layer("A", preset="liquid_glass", bevel=0.08, thickness=0.16)
    hidden["visible"] = False
    cases = {"empty": T.scene([], {}, color_mode="brand"),
             "hidden": T.scene([hidden], {"A": T.geo([("e", [T.circle(0.3)], "#ffffff", 1.0)])}, color_mode="brand")}
    for case, scn in cases.items():
        got = {}
        for q in ("draft", "preview"):
            out = outdir / f"bare_plate_{appearance}_{case}_{q}.png"
            render(worker, scn, out, quality=q, appearance=appearance)
            got[q] = [_lstar(rgb_at(out, x, y, r=6)) for x, y in ((-0.5, 0.4), (0.0, 0.0), (0.45, -0.45))]
            if q == "draft":
                probes = worker.result("scene_info")["probes"]
                assert "BIS Probe" in probes and "BIS Glyph Probe" not in probes, (case, probes)
        tol = 10.0 if appearance == "clear-dark" else 5.0
        for d, p in zip(got["draft"], got["preview"]):
            assert abs(d - p) < tol, (case, got)


@needs_blender
@pytest.mark.parametrize("appearance", ["clear-dark", "clear-light", "tinted-light"])
def test_clear_glyph_drafts_see_the_frosted_plate(worker, outdir, appearance):
    """QA r10 N7: clear-dark drafts rendered glass glyphs near-black because they refracted the dark wallpaper straight through
    the frosted plate (Gemini (32,35,46) vs Cycles' (70,71,79), Photos (34,38,54) vs (82,84,93)); clear-light / tinted-light
    drafts read 7-9 L* dark overall. A domed clear glass glyph on the frosted plate: the draft keeps Cycles' glyph-to-plate
    contrast and stays within a few L* of the Cycles preview (HEAD clear-dark: the glyph 16 L* darker than Cycles' and
    darker than its plate, where Cycles' is brighter)."""
    lay = T.layer("A", preset="liquid_glass", bevel=0.08, thickness=0.16)
    lay["depth"]["inflate"] = 0.25
    scn = T.scene([lay], {"A": T.geo([("e", [T.circle(0.35)], "#ffffff", 1.0)], safe=0.35)}, color_mode="brand")
    got = {}
    for q in ("draft", "preview"):
        out = outdir / f"clear_glyph_{appearance}_{q}.png"
        render(worker, scn, out, quality=q, appearance=appearance)
        got[q] = (_lstar(rgb_at(out, 0.0, 0.0, r=6)), _lstar(rgb_at(out, -0.72, 0.0, r=4)))
    (gd, pd), (gp, pp) = got["draft"], got["preview"]
    assert (gd - pd) > (gp - pp) - 6.0, got          # the glyph shows the plate, not a dark hole in it
    tol = 8.0 if appearance == "clear-dark" else 4.0        # EEVEE's frosted glass has no multiple scattering
    assert gd > gp - tol, got
    assert abs(pd - pp) < (10.0 if appearance == "clear-dark" else 4.0), got    # HEAD clear-light plate: 7 L* dark


@needs_blender
@pytest.mark.parametrize("kind,casts", [("physical", True), ("none", False), ("neutral", True), ("chromatic", True)])
def test_shadows_are_real_on_or_off(worker, outdir, kind, casts):
    render(worker, two_discs(shadow=kind), outdir / f"shadow_{kind}.png")
    objs = {o["name"]: o for o in worker.result("scene_info")["objects"]}
    assert objs["BIS A r0"]["visibleShadow"] is casts and objs["BIS A r1"]["visibleShadow"] is casts


def disc_on_plate(preset, colour, plate, params=None, z=0.0, shadow="physical", r=0.32, thickness=0.24):
    lay = T.layer("A", z=z, preset=preset, params=params, bevel=thickness / 2, thickness=thickness)
    lay["shadow"] = {"kind": shadow, "opacity": 0.5}
    g = T.geo([("e", [T.circle(r)], colour, 1.0)], safe=r)
    return T.scene([lay], {"A": g}, plate_fill=plate, color_mode="brand")


def rgb_at(path, x, y, r=4):
    return T.patch(T.rgba(path), x, y, r)[..., :3].reshape(-1, 3).mean(axis=0)


@needs_blender
def test_clear_glass_shows_the_plate_beneath(worker, outdir):
    """Contacts reference: a white glyph in clear glass reads as the ORANGE plate refracted through thick glass,
    with no milky body, no black (light-blocking) interior."""
    out = outdir / "clear_on_orange.png"
    render(worker, disc_on_plate("clear_glass", "#ffffff", "#ff7c3b"), out, quality="preview")
    plate = rgb_at(out, -0.75, 0.6)
    glass = rgb_at(out, 0.0, 0.05)
    assert glass[0] > glass[1] + 40 > glass[2] + 40, glass.round()           # orange seen through the glass
    assert glass.sum() > 0.35 * plate.sum(), (glass.round(), plate.round())   # light passes (caustics), not a black lens


@needs_blender
def test_frosted_glass_carries_the_art_colour(worker, outdir):
    """Gemini reference: Base Color = the art, transmission 1, roughness 0.267, i.e. frosted tinted glass."""
    out = outdir / "frosted_blue.png"
    render(worker, disc_on_plate("frosted_glass", "#4466ff", "#ffffff", z=0.1), out, quality="preview")
    c = rgb_at(out, 0.0, 0.0)
    assert c[2] > c[0] + 50 and c[2] > 120, c.round()


@needs_blender
def test_dark_rendition_glyph_reads_over_the_dark_plate(worker, outdir):
    """The dark rendition of a liquid-glass glyph: lit (subsurface) glass in the art colour, clearly brighter and
    bluer than the system-dark plate (the clear glass of HEAD read (28, 31, 46) on a (42, 42, 44) plate)."""
    out = outdir / "dark_glyph.png"
    render(worker, disc_on_plate("liquid_glass", "#4466ff", "#ffffff", thickness=0.1), out, quality="preview",
           appearance="dark")
    glyph, plate = rgb_at(out, 0.0, 0.0), rgb_at(out, -0.75, 0.6)
    assert plate.mean() < 90, plate.round()                                  # the plate is dark
    assert glyph[2] > plate[2] + 60 and glyph[2] > glyph[0] + 40, (glyph.round(), plate.round())


@needs_blender
def test_shadow_is_physical_and_falls_away_from_the_light(worker, outdir):
    """Light from the top-left (−45°): an opaque disc floating above a white plate darkens the plate at its lower
    right; with shadows off the same spot stays lit."""
    got = {}
    for kind in ("physical", "none"):
        out = outdir / f"cast_{kind}.png"
        render(worker, disc_on_plate("satin", "#888888", "#b4b4b4", z=0.25, shadow=kind, r=0.25, thickness=0.08),
               out, quality="preview")
        got[kind] = (rgb_at(out, 0.42, -0.42).mean(), rgb_at(out, -0.55, 0.55).mean())
    shade, lit = got["physical"]
    assert lit - shade > 25, got                     # (includes the key's falloff across the plate)
    assert got["none"][0] - shade > 12, got          # the cast shadow itself


@needs_blender
def test_emission_blooms_in_the_compositor(worker, outdir):
    render(worker, disc_on_plate("neon", "#22d3ee", "#202028"), outdir / "neon.png")
    assert "Bloom" in (worker.result("scene_info")["compositor"]["group"] or "")
    render(worker, disc_on_plate("flat", "#22d3ee", "#202028"), outdir / "flat.png")
    assert "Bloom" not in (worker.result("scene_info")["compositor"]["group"] or "")


@needs_blender
def test_missing_colour_mode_renders_brand_with_the_soft_clip(worker, outdir):
    proj, bundle = T.plate_scene("#ff3b00")
    proj["render"].pop("colorMode")
    r = render(worker, (proj, bundle), outdir / "default_cm.png")
    assert r["colorMode"] == "brand"
    comp = worker.result("scene_info")["compositor"]
    assert comp["viewTransform"] == "Standard" and comp["useCompositing"] and "Clip" in comp["group"]


INSPECT = r'''
import bpy, json
out = {}
for m in bpy.data.materials:
    if not m.get("bis_shape") or m.node_tree is None:
        continue
    nt = m.node_tree
    outs = [n for n in nt.nodes if n.bl_idname == "ShaderNodeOutputMaterial"]
    bsdf = [n for n in nt.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"]
    surf = [lk.from_node.bl_idname for o in outs for lk in o.inputs["Surface"].links]
    out[m.name] = {"types": sorted(n.bl_idname for n in nt.nodes), "targets": [o.target for o in outs],
                   "surface": surf, "labels": sum(1 for n in nt.nodes if n.label),
                   "xs": sorted({round(n.location.x) for n in nt.nodes}),
                   "users": [ob.name for ob in bpy.data.objects if ob.active_material == m]}
print("INSPECT " + json.dumps(out))
'''


@needs_blender
def test_saved_blend_has_one_principled_per_shape(worker, photos, outdir):
    """'Open in Blender': every shape material is pre-processing → ONE Principled BSDF → Material Output (All),
    laid out left to right and labelled, so a Blender user adjusts the Principled inputs directly."""
    fx = copy.deepcopy(photos)
    looks = ["brushed_metal", "iridescent", "frosted_glass", "candy"]
    for i, L in enumerate(fx["project"]["layers"]):
        L["material"] = {"preset": looks[i % len(looks)], "params": {"specularTint": 0.3} if i == 0 else {}}
    blend = outdir / "materials.blend"
    worker.result("save_blend", {"project": fx["project"], "geometryPath": fx["geometryPath"], "out": str(blend)})
    script = outdir / "inspect_materials.py"
    script.write_text(INSPECT, encoding="utf-8")
    p = subprocess.run([str(wc.BLENDER), "-b", "--factory-startup", str(blend), "--python", str(script)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    line = next(ln for ln in p.stdout.splitlines() if ln.startswith("INSPECT "))
    mats = json.loads(line[8:])
    assert len(mats) >= 5 and "BIS Plate" in mats
    for name, m in mats.items():
        assert m["types"].count("ShaderNodeBsdfPrincipled") == 1, (name, m["types"])
        assert not FORBIDDEN & set(m["types"]), (name, m["types"])
        assert m["targets"] == ["ALL"] and m["surface"] == ["ShaderNodeBsdfPrincipled"], (name, m)
        assert len(m["types"]) <= MAX_NODES + 1, (name, m["types"])
        assert m["labels"] >= 3 and len(m["xs"]) >= 3, (name, m)          # labelled, laid out in columns
        assert len(m["users"]) == 1, (name, m["users"])                   # one shape per material

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
    """Touching opaque pieces render as ONE body (scene.touching_opaque) — unless a shape has its own material:
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
    """EEVEE draws the same graph: raytraced refraction on transmissive shapes, alpha-blended translucent opaque ones."""
    render(worker, two_discs(preset="clear_glass", elem={"e2": {"preset": "satin", "params": {"alpha": 0.5}}}),
           outdir / "eevee.png")
    mats = worker.result("scene_info")["materials"]
    assert mats["BIS Discs / e1"]["raytraceRefraction"] is True and mats["BIS Discs / e1"]["renderMethod"] == "DITHERED"
    assert mats["BIS Discs / e2"]["raytraceRefraction"] is False and mats["BIS Discs / e2"]["renderMethod"] == "BLENDED"


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
    """Contacts reference: a white glyph in clear glass reads as the ORANGE plate refracted through thick glass —
    no milky body, no black (light-blocking) interior."""
    out = outdir / "clear_on_orange.png"
    render(worker, disc_on_plate("clear_glass", "#ffffff", "#ff7c3b"), out, quality="preview")
    plate = rgb_at(out, -0.75, 0.6)
    glass = rgb_at(out, 0.0, 0.05)
    assert glass[0] > glass[1] + 40 > glass[2] + 40, glass.round()           # orange seen through the glass
    assert glass.sum() > 0.35 * plate.sum(), (glass.round(), plate.round())   # light passes (caustics), not a black lens


@needs_blender
def test_frosted_glass_carries_the_art_colour(worker, outdir):
    """Gemini reference: Base Color = the art, transmission 1, roughness 0.267 — frosted tinted glass."""
    out = outdir / "frosted_blue.png"
    render(worker, disc_on_plate("frosted_glass", "#4466ff", "#ffffff", z=0.1), out, quality="preview")
    c = rgb_at(out, 0.0, 0.0)
    assert c[2] > c[0] + 50 and c[2] > 120, c.round()


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

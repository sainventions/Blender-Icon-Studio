"""Light-angle rig + procedural studio world, ported to PLAN D1 axes (icon in XY, camera on +Z).

One light angle drives everything (glass doc §6.2):  key softbox disk, a grazing rim strip on the lit side,
a weaker opposite rim strip, a front fill and the world's gradient/softbox. ``light_dir(a, e)`` =
(0,0,1)·cos e + (sin a, cos a, 0)·sin e  — angle 0 = from the top (+Y), +90 = from the right (+X).
No HDRI files are used.
"""
from __future__ import annotations

import math
from typing import Optional

import bpy
from mathutils import Matrix, Vector

from . import presets as P
from .nodes import Graph, TopologyMismatch, auto_layout
from .util import clamp, hex_to_linear, lerp, light_dir

K_BASE = 350.0            # W, key energy at distance 6 (tuned for a ±1 icon)
# Exposure calibration (QA round 2, #12): with the uncalibrated rig a white diffuse face facing the camera read
# ~1.28 × its albedo, so under Khronos PBR Neutral every bright brand colour was pushed into the highlight
# compression (desaturated toward white: Brave #fc3a00 rendered #f75845, mean plate ΔE76 6.9). The key + fill
# (diffuse) energy and the world (diffuse wash + coat sheen on every face) are scaled so a face-on satin plate
# reads ≈ its SVG colour; the grazing rim strips (edge highlights on glass) keep their energy.
DIFFUSE_CAL = 0.85
WORLD_CAL = 0.65
# Per-engine calibration (round 4): the same rig lit a face-on satin plate at 1.04 x its albedo in Cycles but
# 0.95 x in EEVEE (world probe + light falloff differ), so drafts read ~10 % darker than previews. The lights
# are scaled per engine so a face-on diffuse surface reads ~1.0 x albedo (+ a small coat reflection) in both;
# materials.py pre-compensates paint colours against that response (DIFFUSE_A/B). The world is left alone
# (changing it makes EEVEE re-bake its probes on every draft <-> preview switch).
ENGINE_CAL = {"CYCLES": 0.95, "BLENDER_EEVEE": 1.075}
RIG = (
    # name, angle offset, elevation (None = lighting.elevation), distance, shape, size, size_y, energy factor
    ("BIS Key", 0.0, None, 6.0, "DISK", 4.0, 4.0, 1.0),
    ("BIS RimTop", 0.0, 82.0, 5.0, "RECTANGLE", 6.0, 0.4, 0.5),
    ("BIS RimOpposite", 180.0, 82.0, 5.0, "RECTANGLE", 6.0, 0.4, 0.15),
    ("BIS Fill", 160.0, 55.0, 6.0, "RECTANGLE", 5.0, 5.0, 0.15),
)
WARM = (1.0, 0.82, 0.64)
COOL = (0.72, 0.84, 1.0)


def resolve(lighting: dict, env_scale: float = 1.0, key_scale: float = 1.0,
            angle_override: Optional[float] = None) -> dict:
    """Effective rig parameters from Project.lighting + its preset (+ appearance multipliers).

    ``angle_override`` (light-sweep animation) wins over the preset's ``lockAngle``."""
    pre = P.lighting(str(lighting.get("preset", "studio")))
    angle = float(pre["lockAngle"]) if "lockAngle" in pre else float(lighting.get("angle", -45.0))
    if angle_override is not None:
        angle = float(angle_override)
    return {
        "angle": angle,
        "elevation": clamp(float(lighting.get("elevation", 50.0)), 0.0, 89.0),
        "key": float(pre.get("key", 1.0)) * float(lighting.get("intensity", 1.0)) * key_scale,
        "rim": float(pre.get("rim", 1.0)) * float(lighting.get("rim", 1.0)),
        "fill": float(pre.get("fill", 1.0)) * float(lighting.get("fill", 1.0)),
        "environment": float(pre.get("environment", 1.0)) * float(lighting.get("environment", 1.0)) * env_scale,
        "warmth": float(pre.get("warmth", 0.0)),
        "softness": clamp(float(lighting.get("shadowSoftness", 0.5))),
        "rimColors": [hex_to_linear(c) for c in pre.get("rimColors", [])],
        "intensity": float(lighting.get("intensity", 1.0)),
    }


def key_vector(rig: dict) -> tuple:
    return light_dir(rig["angle"], rig["elevation"])


def _look_rotation(d: Vector) -> "Matrix":
    """Rotation whose −Z axis points along −d (toward the origin) — glass doc §6.2 orientation."""
    return (-d).to_track_quat("-Z", "Y").to_matrix().to_4x4()


def engine_cal(engine: Optional[str]) -> float:
    return ENGINE_CAL.get(str(engine or "CYCLES"), 1.0)


def update_lights(scene: bpy.types.Scene, collection: bpy.types.Collection, rig: dict,
                  engine: Optional[str] = None) -> list:
    """Create / update the 4-light rig (in place: no datablock churn while dragging the angle dial).
    ``engine`` (render engine id) selects the per-engine exposure calibration (ENGINE_CAL)."""
    objs = []
    soft = 0.3 + 1.4 * rig["softness"]
    cal = engine_cal(engine)
    for i, (name, d_angle, elev, dist, shape, size, size_y, efac) in enumerate(RIG):
        e = rig["elevation"] if elev is None else elev
        d = Vector(light_dir(rig["angle"] + d_angle, e))
        ob = bpy.data.objects.get(name)
        if ob is None or ob.type != "LIGHT":
            ld = bpy.data.lights.new(name, "AREA")
            ob = bpy.data.objects.new(name, ld)
        if ob.name not in collection.objects:
            collection.objects.link(ob)
        ld = ob.data
        ld.shape = shape
        if name == "BIS Key":
            ld.size = size * soft
            ld.size_y = size * soft
            energy = K_BASE * DIFFUSE_CAL * rig["key"]
            col = lerp((1.0, 1.0, 1.0), WARM, rig["warmth"])
        elif name == "BIS Fill":
            ld.size = size
            ld.size_y = size_y
            energy = K_BASE * DIFFUSE_CAL * efac * rig["fill"]
            col = lerp((1.0, 1.0, 1.0), COOL, rig["warmth"])
        else:
            ld.size = size
            ld.size_y = size_y
            energy = K_BASE * efac * rig["rim"] * max(0.4, rig["intensity"])
            col = (1.0, 1.0, 1.0)
            if rig["rimColors"]:
                col = rig["rimColors"][min(i - 1, len(rig["rimColors"]) - 1)]
        energy *= cal
        if abs(ld.energy - max(0.0, energy)) > 1e-6:
            ld.energy = max(0.0, energy)
        ld.color = col
        ld.use_shadow = True
        try:
            ld.use_soft_falloff = True
        except AttributeError:
            pass
        ob.matrix_world = Matrix.Translation(d * dist) @ _look_rotation(d)
        ob.hide_render = energy <= 0.0
        objs.append(ob)
    return objs


# ------------------------------------------------------------------------------------------------
# world
# ------------------------------------------------------------------------------------------------
GRADIENT = [(0.00, (0.02, 0.02, 0.025), 1.0), (0.55, (0.18, 0.18, 0.20), 1.0),
            (0.80, (0.60, 0.60, 0.62), 1.0), (1.00, (1.0, 1.0, 1.0), 1.0)]


def _world_graph(g: Graph, rig: dict, backdrop: tuple, strength_scale: float = 1.0) -> None:
    a = math.radians(rig["angle"])
    up = (math.sin(a), math.cos(a), 0.0)
    L = light_dir(rig["angle"], 50.0)
    tc = g.node("ShaderNodeTexCoord")
    d = g.vmath("NORMALIZE", tc.outputs["Generated"])
    gy = g.vmath("DOT_PRODUCT", d, up)
    grad = g.ramp(g.map_range(gy, -1.0, 1.0, 0.0, 1.0), GRADIENT)
    soft = g.map_range(g.vmath("DOT_PRODUCT", d, L), 0.90, 0.97, 0.0, 6.0 * max(0.2, rig["key"]), interp="SMOOTHSTEP")
    front = g.map_range(g.vmath("DOT_PRODUCT", d, (0.0, 0.0, 1.0)), 0.0, 1.0, 0.0, 0.5 * max(0.3, rig["fill"]))
    add = g.math("ADD", soft, front)
    col = g.mix_rgb(1.0, grad.outputs["Color"], add, blend="ADD")
    tint = lerp((1.0, 1.0, 1.0), lerp(WARM, (1, 1, 1), 0.5), rig["warmth"] * 0.6)
    col = g.mix_rgb(1.0, col, tint, blend="MULTIPLY")
    bg = g.node("ShaderNodeBackground")
    g.set(bg.inputs["Color"], col)
    g.set(bg.inputs["Strength"], 0.6 * WORLD_CAL * rig["environment"] * strength_scale)
    cam = g.node("ShaderNodeBackground")
    g.set(cam.inputs["Color"], backdrop)
    g.set(cam.inputs["Strength"], 1.0)
    lp = g.node("ShaderNodeLightPath")
    mix = g.mix_shader(lp.outputs["Is Camera Ray"], bg.outputs[0], cam.outputs[0])
    out = g.node("ShaderNodeOutputWorld")
    g.link(mix, out.inputs["Surface"])


def update_world(scene: bpy.types.Scene, rig: dict, backdrop_rgb: tuple = (0.05, 0.05, 0.06),
                 engine: Optional[str] = None) -> bpy.types.World:
    """Procedural studio world. Not engine-calibrated (``engine`` is accepted for symmetry): a world change
    makes EEVEE re-bake its probes, ~50 ms on every draft <-> preview switch; the lights carry ENGINE_CAL."""
    world = bpy.data.worlds.get("BIS World")
    fresh = world is None
    if fresh:
        world = bpy.data.worlds.new("BIS World")
    scene.world = world
    nt = world.node_tree
    if not fresh and world.get("bis_key") == "v1":
        try:
            _world_graph(Graph(nt, update=True), rig, backdrop_rgb)
            return world
        except TopologyMismatch:
            pass
    nt.nodes.clear()
    _world_graph(Graph(nt), rig, backdrop_rgb)
    world["bis_key"] = "v1"
    auto_layout(nt)
    return world

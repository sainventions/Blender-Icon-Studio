"""Scene construction: (project, geometry bundle, appearance) -> Blender scene, incrementally.

Layout (PLAN §3, D1): icon in the XY plane, camera on +Z looking −Z, 1 BU = 1 art unit.

    BIS Icon (collection)
      BIS Plate                       canvas space; back at −thickness, front face at z = 0
      BIS Art          (empty)        canvas.art: uniform scale + (x, y)
        BIS Layer <id> (empty)        layer.transform (scale about the canvas origin, then translate)
                                      + z = depth.z + ε (REAL distances; camera.explode is legacy, ignored)
          BIS <id> r<i> / sil / img<k>  height-field body meshes (heightfield.piece_mesh); object coords == art
                                      coords, mid-plane at z + thickness/2 (+ lift: an inflated layer's lowest
                                      point stays on z). Pieces of one layer never interpenetrate: touching ones
                                      pull back INSET_GAP, overlapping ones stack by real height (_relations)
    BIS Rig (collection)              camera (front / CAD iso orthographic / perspective), then the light rig and
                                      the world turned with the camera (camera-relative lighting, round 7), wallpaper,
                                      BIS Probe: EEVEE sphere probe of the plate (refraction fallback, round 8)
                                      BIS Glyph Probe: the EEVEE probe of the layers' space for glass plates (round 9)

Everything is updated in place between renders: objects/empties are reused by name, piece meshes come from
heightfield's cache (layer hash + depth params), materials update their node values in place.
"""
from __future__ import annotations

import math
import os
from typing import Optional

import bpy
import numpy as np
from mathutils import Matrix, Vector

from . import appearance as appearance_mod
from . import framing, geometry, heightfield, lighting, materials
from .defaults import norm_bundle, norm_camera, norm_project
from .gpu import configure_scene
from .util import gradient_samples, hex_to_linear, hex_to_srgb, log, srgb_to_linear, stable_hash

LAYER_EPS = 0.002          # layer back face above the plate front face
REGION_DZ = 0.001          # zSub step (zSub is interpreted as a sub-layer index)
CARD_THICKNESS = 0.004
PLATE_WALLPAPER_GAP = 0.9  # wallpaper plane below the plate's back face
STACK_GAP = 0.002          # overlapping pieces of one layer are stacked by their real heights, this far apart
INSET_GAP = 0.003          # touching pieces of one layer: the lower one pulls back this far from the upper one
TOUCH_TOL = 0.0018         # world: pieces closer than this (3 x heightfield.CHORD_TOL) touch
PROBE_NAME = "BIS Probe"   # EEVEE sphere light probe of the plate (see SceneBuilder._probe)
PROBE_Z = 0.02             # world: its capture point, just above the plate's front face (z = 0)
PROBE_REACH = 1.5          # its influence radius beyond the subject's top / the plate (world units)
GLYPH_PROBE_NAME = "BIS Glyph Probe"   # glass plates: the probe of the space above the plate (SceneBuilder._probe)
PLATE_PROBE_CLIP = 0.4     # glass plates: the plate probe sits mid-plate and clips this far past the plate's faces
GLYPH_PROBE_MARGIN = 0.1   # the glyph probe's box reaches this far beyond the layers' tops / sides (world)
GLYPH_PROBE_FALLOFF = 0.02  # its box influence fades over this fraction of its size (a sharp floor above the plate)
GLYPH_PROBE_FLOOR = 0.03   # world z of its floor: flat cards (0.02 thick) and the bodies' lowest rims stay with the plate
LIGHT_GLASS_TRACE = 0.3    # drafts with a glass plate over a LIGHT wallpaper: EEVEE trace_max_roughness (_probe)
FRONT_TILT = 2e-4          # rad: head-on cameras are pitched this much about the world origin (see SceneBuilder._camera)


def _collection(name: str, parent: Optional[bpy.types.Collection] = None) -> bpy.types.Collection:
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
    parent = parent or bpy.context.scene.collection
    if col.name not in parent.children:
        parent.children.link(col)
    return col


def _empty(name: str, col: bpy.types.Collection, parent: Optional[bpy.types.Object] = None) -> bpy.types.Object:
    ob = bpy.data.objects.get(name)
    if ob is None or ob.type != "EMPTY":
        ob = bpy.data.objects.new(name, None)
        ob.empty_display_size = 0.2
    if ob.name not in col.objects:
        col.objects.link(ob)
    if ob.parent != parent:
        ob.parent = parent
        ob.matrix_parent_inverse = Matrix.Identity(4)
    return ob


def _mesh_object(name: str, data: bpy.types.Mesh, col: bpy.types.Collection,
                 parent: Optional[bpy.types.Object]) -> bpy.types.Object:
    """Object showing a cached body mesh (heightfield.piece_mesh). No modifiers: nothing is re-evaluated
    per render (saved .blend files get the same plain meshes)."""
    ob = bpy.data.objects.get(name)
    if ob is not None and ob.type != "MESH":
        bpy.data.objects.remove(ob)
        ob = None
    if ob is None:
        ob = bpy.data.objects.new(name, data)
    elif ob.data != data:
        old = ob.data
        ob.data = data
        if old is not None and old.users == 0 and not heightfield.is_cached_mesh(old):
            bpy.data.meshes.remove(old)
    for m in list(ob.modifiers):
        ob.modifiers.remove(m)
    if ob.name not in col.objects:
        col.objects.link(ob)
    if ob.parent != parent:
        ob.parent = parent
        ob.matrix_parent_inverse = Matrix.Identity(4)
    return ob


def _set_material(ob: bpy.types.Object, mat: bpy.types.Material) -> None:
    if len(ob.material_slots) == 0:
        ob.data.materials.append(None)
    slot = ob.material_slots[0]
    if slot.link != "OBJECT":
        slot.link = "OBJECT"
    if slot.material != mat:
        slot.material = mat


def _iso(cam: dict) -> float:
    """camera.iso clamped to 0..1 (0 = head-on, 1 = isometric; PLAN §11 View)."""
    try:
        return max(0.0, min(1.0, float(cam.get("iso", 0.0) or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def geo_key(g: dict, lid: str) -> str:
    """Cache identity of a layer's geometry: A's content hash, or (hand-written bundles without a hash) a
    hash of the splines themselves. Never just the layer id: ids like 'L0' repeat across projects and a
    cached curve of another icon would be reused."""
    h = g.get("hash")
    if h:
        return str(h)
    if "_bis_key" not in g:
        g["_bis_key"] = "c-" + stable_hash([g.get("silhouette"), [r.get("splines") for r in g.get("regions") or []]])
    return g["_bis_key"]


# ------------------------------------------------------------------------------------------------
# paint specs
# ------------------------------------------------------------------------------------------------
def paint_spec(fill: dict, texture: Optional[str], bbox=(-1.0, -1.0, 1.0, 1.0),
               region_paint: Optional[dict] = None) -> dict:
    """Layer/plate fill -> material paint spec (object coordinates of the target object). 'auto' = the layer's
    rasterised art texture, else (hand-written bundles) the shape's own region paint."""
    t = (fill or {}).get("type", "auto")
    if t == "auto":
        if texture and os.path.isfile(texture):
            return {"kind": "texture", "image": texture}
        if region_paint and region_paint.get("type") not in (None, "auto", "none"):
            return paint_spec(region_paint, None, bbox)
        return {"kind": "solid", "color": (1.0, 1.0, 1.0), "has_alpha": False}
    if t == "solid":
        return {"kind": "solid", "color": hex_to_linear(fill.get("color", "#ffffff")),
                "has_alpha": False}
    if t in ("system-light", "system-dark"):
        a, b = ("#ffffff", "#e4e5ea") if t == "system-light" else ("#3a3a3f", "#111114")
        stops = [{"offset": 0.0, "color": a}, {"offset": 1.0, "color": b}]
        return {"kind": "linear", "start": (0.0, bbox[3]), "end": (0.0, bbox[1]),
                "samples": gradient_samples(stops), "has_alpha": False}
    if t == "linear":
        stops = fill.get("stops") or []
        return {"kind": "linear", "start": tuple(fill.get("start", (0, 1))), "end": tuple(fill.get("end", (0, -1))),
                "samples": gradient_samples(stops), "has_alpha": any(float(s.get("opacity", 1)) < 0.999 for s in stops)}
    if t == "radial":
        stops = fill.get("stops") or []
        return {"kind": "radial", "center": tuple(fill.get("center", (0, 0))), "radius": float(fill.get("radius", 1)),
                "focal": fill.get("focal"), "matrix": fill.get("matrix"), "samples": gradient_samples(stops),
                "has_alpha": any(float(s.get("opacity", 1)) < 0.999 for s in stops)}
    return {"kind": "solid", "color": (1.0, 1.0, 1.0), "has_alpha": False}   # 'none'


def image_uv(im: dict, fallback_bbox) -> tuple:
    """UV affine of a raster image (``LayerGeometry.images[]``) in object (= art) coordinates:
    ``((au, bu, cu), (av, bv, cv))`` with ``u = au·x + bu·y + cu`` and ``v = av·x + bv·y + cv``.

    A's cards carry ``matrix`` (image pixel space, y down -> art space) + ``width``/``height``: that is the
    exact placement, including rotation/skew. ``bbox`` is only the bbox of the visible (alpha-traced,
    clipped) silhouette, so stretching the PNG over it misplaces images with transparent margins. Without
    a matrix the bbox is the best available placement."""
    m = im.get("matrix")
    w, h = im.get("width"), im.get("height")
    try:
        if m and len(m) == 6 and w and h:
            a, b, c, d, e, f = (float(v) for v in m)
            W, H = float(w), float(h)
            det = a * d - b * c
            if abs(det) > 1e-18 and W > 0 and H > 0:
                # pixel = inv(M) · (art - t);  u = px / W;  v = 1 - py / H  (Blender images: v = 0 at the bottom)
                pxx, pxy, pxc = d / det, -c / det, (c * f - d * e) / det
                pyx, pyy, pyc = -b / det, a / det, (b * e - a * f) / det
                return ((pxx / W, pxy / W, pxc / W), (-pyx / H, -pyy / H, 1.0 - pyc / H))
    except (TypeError, ValueError):
        pass
    x0, y0, x1, y1 = (float(v) for v in (im.get("bbox") or fallback_bbox))
    w_, h_ = max(1e-6, x1 - x0), max(1e-6, y1 - y0)
    return ((1.0 / w_, 0.0, -x0 / w_), (0.0, 1.0 / h_, -y0 / h_))


def image_quad(im: dict, fallback_bbox) -> list:
    """Art-space corners (CCW) of a raster card's full placement (matrix) or its bbox."""
    m = im.get("matrix")
    w, h = im.get("width"), im.get("height")
    try:
        if m and len(m) == 6 and w and h:
            a, b, c, d, e, f = (float(v) for v in m)
            W, H = float(w), float(h)
            pts = [(a * px + c * py + e, b * px + d * py + f) for px, py in ((0, H), (W, H), (W, 0), (0, 0))]
            area2 = sum(pts[i - 1][0] * pts[i][1] - pts[i][0] * pts[i - 1][1] for i in range(4))
            if abs(area2) > 1e-12:
                return pts if area2 > 0 else pts[::-1]
    except (TypeError, ValueError):
        pass
    x0, y0, x1, y1 = (float(v) for v in (im.get("bbox") or fallback_bbox))
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


PLATE_MATERIAL = "BIS Plate"
WALLPAPER_MATERIAL = "BIS Wallpaper"


def touching_opaque(g: dict, images: Optional[dict] = None) -> bool:
    """True when some regions of a layer share edges (the union silhouette has fewer outer contours than the
    regions together) and every region is opaque vector paint; the layer then renders as one body."""
    regions = g.get("regions") or []
    if len(regions) < 2 or not g.get("silhouette"):
        return False
    if images or g.get("images"):
        return False
    if any(float(r.get("opacity", 1.0)) < 0.999 or float(r.get("zSub", 0.0) or 0.0) != 0.0 for r in regions):
        return False
    if any(float(st.get("opacity", 1.0)) < 0.999 for r in regions for st in (r.get("paint") or {}).get("stops") or []):
        return False
    sil_outer = sum(1 for s in g["silhouette"] if not s.get("hole"))
    reg_outer = sum(1 for r in regions for s in r.get("splines") or [] if not s.get("hole"))
    return 0 < sil_outer < reg_outer


def paint_rgb(paint: dict) -> tuple:
    """Representative LINEAR colour of a region paint (object-colour fallback, previews)."""
    t = paint.get("type")
    if t == "solid":
        return hex_to_linear(paint.get("color", "#ffffff"))
    if t in ("linear", "radial") and paint.get("stops"):
        cols = [hex_to_srgb(s.get("color", "#000")) for s in paint["stops"]]
        avg = [sum(c[i] for c in cols) / len(cols) for i in range(3)]
        return tuple(srgb_to_linear(c) for c in avg)
    return (1.0, 1.0, 1.0)


# ------------------------------------------------------------------------------------------------
# builder
# ------------------------------------------------------------------------------------------------
class SceneBuilder:
    """Owns the BIS objects in the current scene and rebuilds them incrementally."""

    def __init__(self):
        self.warnings: list[str] = []
        self.info: dict = {}
        self._specs: list[dict] = []
        self.editable = False
        self._hidden: set = set()
        self._cover_cache: dict = {}
        self._touch_cache: dict = {}
        self._shape_names: set = set()

    # -------------------------------------------------------------------------- setup
    @property
    def scene(self) -> bpy.types.Scene:
        return bpy.context.scene

    def reset(self) -> None:
        """Empty file (keeps preferences: OptiX) + fresh collections."""
        bpy.ops.wm.read_homefile(use_empty=True)
        configure_scene(self.scene)
        heightfield.reset_caches()
        self.info = {}

    def collections(self):
        icon = _collection("BIS Icon")
        rig = _collection("BIS Rig")
        return icon, rig

    # -------------------------------------------------------------------------- main entry
    def build(self, project: dict, bundle: dict, appearance: str = "light", *, camera: Optional[dict] = None,
              full_bleed: bool = False, backdrop: Optional[str] = None, backdrop_color: Optional[str] = None,
              overrides: Optional[dict] = None, editable: bool = False, engine: Optional[str] = None) -> dict:
        """Build/update the scene. ``overrides`` (animations): {'lightAngle', 'camera', 'layerZ': {id: dz},
        'framing': one shared framing plan}. ``editable`` (save_blend): kept for the API; renders and .blend
        files use the same plain body meshes. ``engine`` (render engine id of the tier about to render)
        selects the per-engine light calibration (lighting.ENGINE_CAL)."""
        self.warnings = []
        self.editable = bool(editable)
        overrides = overrides or {}
        proj = norm_project(project)
        bnd = norm_bundle(bundle)
        res = appearance_mod.resolve(proj, appearance, bnd)
        eff, env = res["project"], res["env"]
        cam = norm_camera(camera, eff["camera"]) if camera else dict(eff["camera"])
        if "camera" in overrides:
            cam.update(overrides["camera"])
        cam["explode"] = 1.0           # legacy (PLAN §11): layers always sit at their REAL z (use camera.iso)
        cam["iso"] = _iso(cam)
        lighting_spec = dict(eff["lighting"])
        rset = eff["render"]
        backdrop = backdrop or rset.get("backdrop", "transparent")
        backdrop_color = backdrop_color or rset.get("backdropColor", "#1c1c22")

        icon_col, rig_col = self.collections()
        scene = self.scene
        self._specs = []
        self._shape_names = set()
        rig = lighting.resolve(lighting_spec, env["envScale"], env["keyScale"], overrides.get("lightAngle"))
        # CAD iso (orthographic) and perspective views are auto-framed on the subject (framing.py); animations
        # pass one shared plan for the whole clip
        fplan = None
        given = overrides.get("framing") or None
        zoom = float(cam.get("zoom", 1.0) or 1.0)
        if not full_bleed and cam.get("view", "front") != "front":
            fplan = given if given and given.get("kind", "persp") == "persp" else None
            if fplan is None:
                pts = self.subject_points(self.subject_hulls(eff, bnd, full_bleed), overrides.get("layerZ") or {})
                fplan = framing.plan([(pts, float(cam.get("tiltX", 0.0)), float(cam.get("tiltY", 0.0)))],
                                     float(cam.get("fov", 30.0) or 30.0), zoom)
        elif not full_bleed and (cam["iso"] > 0.0 or (given and given.get("kind") == "ortho")):
            fplan = given if given and given.get("kind") == "ortho" else None
            if fplan is None:
                pts = self.subject_points(self.subject_hulls(eff, bnd), overrides.get("layerZ") or {}, canvas=True)
                fplan = framing.ortho_plan([(pts, cam["iso"])], zoom)
        cam_ob = self._camera(rig_col, cam, full_bleed, fplan)
        # camera-relative studio (PLAN §11 round 7): the key / rim / fill rig and the world are defined in the camera's
        # frame (lighting.angle is relative to the view), so iso / perspective / animated views light like head-on;
        # without the head-on FRONT_TILT nudge (a numerical guard of the camera only: the rig stays where it was)
        view = cam_ob.matrix_world.to_3x3().normalized()
        if cam_ob.get("bis_tilt"):
            view = Matrix.Rotation(-FRONT_TILT, 3, "X") @ view
        lighting.update_lights(scene, rig_col, rig, engine, view)
        lighting.update_world(scene, rig, (*hex_to_linear(backdrop_color), 1.0), engine, view)

        canvas = eff["canvas"]
        shape = "square" if full_bleed else canvas["shape"]
        keep: set[str] = set()
        used_mats: set[str] = set()

        layers = [Lr for Lr in eff["layers"] if Lr.get("visible", True)]
        geo_by_id = bnd["layers"]

        # ---- pieces the SVG hides completely under opaque higher art (Translate's magenta shadow-caster under
        # the blue card): glass above must not reveal them (they still cast shadows / show their sides) --------
        self._hidden = self._covered_pieces(layers, geo_by_id, canvas["art"])

        # ---- plate ---------------------------------------------------------------------------------
        plate = canvas["plate"]
        plate_ok = plate.get("visible", True) and shape != "none" and (plate["fill"].get("type") != "none")
        wp_kind = env.get("wallpaper") or (("dark" if env.get("dark") else "light") if backdrop == "wallpaper" else None)
        glass_plate = False
        if plate_ok:
            glass_plate = self._plate(icon_col, plate, shape, canvas.get("cornerRadius", 0.225), full_bleed)
            keep.add("BIS Plate")
            used_mats.add(PLATE_MATERIAL)

        # ---- art root --------------------------------------------------------------------------------
        art = canvas["art"]
        sa = max(1e-4, float(art.get("scale", 1.0)))
        art_ob = _empty("BIS Art", icon_col)
        art_ob.location = (float(art.get("x", 0.0)), float(art.get("y", 0.0)), 0.0)
        art_ob.scale = (sa, sa, sa)
        art_ob.rotation_euler = (0.0, 0.0, 0.0)
        keep.add(art_ob.name)

        stats = {"layers": 0, "pieces": 0}
        layer_z = overrides.get("layerZ") or {}
        for Lr in layers:
            g = geo_by_id.get(Lr["id"])
            if g is None:
                self.warnings.append(f"layer {Lr['id']} has no geometry in the bundle")
                continue
            names, mats = self._layer(icon_col, art_ob, Lr, g, sa, env, stats, float(layer_z.get(Lr["id"], 0.0)))
            keep |= names
            used_mats |= mats
            stats["layers"] += 1

        # ---- wallpaper ---------------------------------------------------------------------------------
        if wp_kind:
            self._wallpaper(rig_col, wp_kind, plate["thickness"], camera_visible=(backdrop == "wallpaper"))
            keep.add("BIS Wallpaper")

        # ---- EEVEE: the plate's sphere light probe (what refraction falls back to) [+ a glass plate's glyph probe] ----
        bodies = [ob for ob in icon_col.objects if ob.name in keep and ob.type == "MESH" and ob.name != "BIS Plate"]
        self._probe(rig_col, eff, bnd, sa, plate_ok, glass_plate, layer_z, plate, wp_kind, bodies)

        # ---- cleanup stale BIS objects / materials ---------------------------------------------------
        for ob in list(icon_col.objects) + [o for o in rig_col.objects if o.name == "BIS Wallpaper"]:
            if ob.name not in keep:
                data = ob.data if ob.type == "MESH" else None
                bpy.data.objects.remove(ob, do_unlink=True)
                if data is not None and data.users == 0 and not heightfield.is_cached_mesh(data):
                    bpy.data.meshes.remove(data)
        materials.purge_unused(used_mats)
        heightfield.purge_unused_meshes()
        materials.purge_unused_images()

        # ---- scene-level flags ---------------------------------------------------------------------------
        all_specs = self._specs
        self.info = {
            "appearance": res["appearance"],
            "backdrop": backdrop,
            "backdropColor": backdrop_color,
            "wallpaper": appearance_mod.wallpaper(wp_kind) if wp_kind else None,
            # transparent backdrops: Cycles keeps glass up to this roughness see-through (film_transparent_roughness)
            "maxGlassRoughness": materials.max_glass_roughness(all_specs),
            # EEVEE drafts: trace_max_roughness (None = render.TRACE_MAX_ROUGHNESS). Over a LIGHT wallpaper a glass
            # plate's frosted glyphs trace the plate on screen; over a dark one they read the glyph probe (_probe)
            "traceMaxRoughness": LIGHT_GLASS_TRACE if glass_plate and wp_kind == "light" else None,
            # compositor bloom (a render setting): glowing emission above the flat-art level (strength 1) blooms
            "bloom": max(0.0, min(1.0, (materials.max_emission(all_specs) - 1.0) / 4.0)),
            "fullBleed": bool(full_bleed),
            "camera": cam,
            "light": {"angle": rig["angle"], "elevation": rig["elevation"]},
            "stats": stats,
            "warnings": list(self.warnings),
        }
        return self.info

    # -------------------------------------------------------------------------- camera
    def _camera(self, col: bpy.types.Collection, cam: dict, full_bleed: bool,
                fplan: Optional[dict] = None) -> bpy.types.Object:
        ob = bpy.data.objects.get("BIS Camera")
        if ob is None:
            ob = bpy.data.objects.new("BIS Camera", bpy.data.cameras.new("BIS Camera"))
        if ob.name not in col.objects:
            col.objects.link(ob)
        cd = ob.data
        zoom = max(0.05, float(cam.get("zoom", 1.0)))
        cd.clip_start = 0.05
        cd.clip_end = 200.0
        # head-on views are pitched by FRONT_TILT (0.01°, < 0.05 px at 512) about the world origin: an orthographic view
        # EXACTLY parallel to a flat face's normal makes EEVEE's forward (alpha-blended) refraction NaN: flat
        # translucent glass and flat soft-alpha raster cards rendered as pure black discs in drafts at some art scales
        # (whether the transformed normal stays exactly (0, 0, 1) depends on float rounding; round-8 review)
        tilt = Matrix.Rotation(FRONT_TILT, 4, "X")
        ob["bis_tilt"] = bool(full_bleed or not fplan or (fplan.get("kind") == "ortho" and _iso(cam) <= 0.0))
        if full_bleed or not fplan:
            # front: the fixed App-Store framing (room for shadows); unchanged by auto-framing
            cd.type = "ORTHO"
            cd.ortho_scale = (2.0 if full_bleed else 2.24) / zoom
            cd.shift_x = cd.shift_y = 0.0
            ob.matrix_world = tilt @ Matrix.Translation((0.0, 0.0, 10.0))
        elif fplan.get("kind") == "ortho":
            # CAD iso view: orthographic, rotated head-on -> isometric, real z distances (framing.ortho_plan)
            cd.type = "ORTHO"
            cd.sensor_fit = "AUTO"
            pos, rot = framing.ortho_pose(fplan, _iso(cam))
            cd.ortho_scale = float(fplan["scale"])
            cd.shift_x, cd.shift_y = (float(s) for s in fplan["shift"])
            m = Matrix([list(r) for r in rot]).to_4x4()
            m.translation = Vector([float(c) for c in pos])
            ob.matrix_world = tilt @ m if ob["bis_tilt"] else m
            cd.clip_end = max(200.0, float(fplan.get("clip", 200.0)))
        else:
            cd.type = "PERSP"
            cd.sensor_fit = "AUTO"
            pos, rot = framing.camera_pose(fplan, float(cam.get("tiltX", 0.0)), float(cam.get("tiltY", 0.0)))
            cd.angle = 2.0 * math.atan(max(1e-4, float(fplan["tan"])))
            cd.shift_x, cd.shift_y = (float(s) for s in fplan["shift"])
            m = Matrix([list(r) for r in rot]).to_4x4()
            m.translation = Vector([float(c) for c in pos])
            ob.matrix_world = m
            dist = float(np.linalg.norm(pos - np.asarray(fplan["target"])))
            # near clip well in front of the subject's nearest point (plan['near'], depth along the view axis)
            cd.clip_start = max(0.001, min(dist * 0.05, 0.5 * float(fplan.get("near", dist))))
        self.scene.camera = ob
        return ob

    # -------------------------------------------------------------------------- framing subject
    def subject_hulls(self, eff: dict, bnd: dict, full_bleed: bool = False) -> list:
        """Convex hulls (canvas XY) of everything visible + their REAL z spans, for auto-framing:
        [(hull (M, 2), z0, z1, layer id | None)]. The plate spans −thickness..0; a layer spans its body:
        z + ε .. + max(thickness, 2 × half height) (inflated bodies are taller, see :meth:`_layer`)."""
        canvas = eff["canvas"]
        shape = "square" if full_bleed else canvas["shape"]
        out = []
        plate = canvas["plate"]
        if plate.get("visible", True) and shape != "none" and (plate["fill"].get("type") != "none"):
            th = max(0.0, float(plate.get("thickness", 0.16)))
            ring = [p for s in geometry.plate_outline(shape, canvas.get("cornerRadius", 0.225))
                    for p in geometry._flatten_ring(s["points"], True, 6)]
            out.append((framing.hull2d(np.asarray(ring)), -th, 0.0, None))
        art = canvas["art"]
        sa, ax, ay = float(art.get("scale", 1.0)), float(art.get("x", 0.0)), float(art.get("y", 0.0))
        for Lr in eff["layers"]:
            if not Lr.get("visible", True):
                continue
            g = bnd["layers"].get(Lr["id"])
            if g is None:
                continue
            ring = []
            spl = g.get("silhouette") or [s for r in g.get("regions") or [] for s in r.get("splines") or []]
            for s in spl:
                ring += geometry._flatten_ring(s.get("points") or [], bool(s.get("closed", True)), 4)
            # flat image cards (raster images that are not extruded regions) render their full placement quad;
            # an extruded raster region is bounded by its traced contour (its PNG placement can overhang the
            # plate: Find Device's 1685 px image framed 8 % too loose and off-centre)
            region_ids = {r.get("elementId") for r in g.get("regions") or []}
            for im in g.get("images") or []:
                if im.get("path") and im.get("elementId") not in region_ids:
                    ring += [tuple(p) for p in image_quad(im, g.get("bbox") or (-1, -1, 1, 1))]
            if len(ring) < 3:
                continue
            tr = Lr["transform"]
            sl, tx, ty = float(tr.get("scale", 1.0)), float(tr.get("x", 0.0)), float(tr.get("y", 0.0))
            pts = (np.asarray(ring, dtype=np.float64) * sa + np.array([ax, ay])) * sl + np.array([tx, ty])
            z0 = float(Lr["depth"].get("z", 0.0)) + LAYER_EPS
            out.append((framing.hull2d(pts), z0, z0 + self._body_height(Lr, g, sa * sl), Lr["id"]))
        return out

    def _body_height(self, Lr: dict, g: dict, S: float) -> float:
        """World height H of a layer's bodies (PLAN §11 round 8, identical in the server's bis.stacking and the web):
        H = max(rule height, in-layer stacked height). Rule height (presets.json "geometry") = thickness + 2 × inflate ×
        maxRadius × S (LayerGeometry.maxRadius; the worker's own inradius of the silhouette when the bundle has none);
        in-layer stacked height = the real-height stack of the layer's OVERLAPPING pieces (:meth:`_relations`, the same
        stack :meth:`_layer` builds): max_j(shift_j + h_j) + max_j h_j with every body's half height h_j."""
        dp = Lr["depth"]
        th = max(0.0, float(dp.get("thickness", 0.1)))
        k = max(0.0, min(1.0, float(dp.get("inflate", 0.0) or 0.0)))
        b = min(max(0.0, float(dp.get("bevel", 0.045))), th / 2.0)

        def half(spl, key):
            if k <= 0.0:
                return th / 2.0
            return heightfield.half_height(th, b, k, heightfield.inradius(spl, S, key) * S)

        regions = g.get("regions") or []
        mr = g.get("maxRadius")
        if k <= 0.0:
            rule = th
        elif isinstance(mr, (int, float)) and mr > 0:
            rule = th + 2.0 * k * float(mr) * S
        else:
            spl = g.get("silhouette") or [s for r in regions for s in r.get("splines") or []]
            rule = max(th, 2.0 * half(spl, "D-" + geo_key(g, Lr["id"])))
        if Lr.get("mode") != "combined" and len(regions) > 1:
            pids = [f"r{i}" for i in range(len(regions))]
            pairs = self._relations(g, Lr["id"], pids, S)["stack"]
            if pairs:
                hk = "D-" + geo_key(g, Lr["id"])
                hs = [half(r.get("splines") or [], f"{hk}-r{i}") for i, r in enumerate(regions)]
                sh = heightfield.stack_shifts(len(hs), pairs, hs, STACK_GAP)
                return max(rule, float(max(s + h for s, h in zip(sh, hs)) + max(hs)))
        return rule

    def _relations(self, g: dict, lid: str, pids: list, S: float) -> dict:
        """How the pieces of one layer meet (PLAN §11 round 7; pieces = regions 'r<k>' in paint order, cached by the
        layer geometry): {'stack': [(i, j)] where piece j OVERLAPS the earlier piece i (a translucent piece over another;
        A only occlusion-cuts under opaque art): j is stacked on i by their real heights; 'inset': {i: splines} where
        piece i only TOUCHES later pieces along a shared edge (Secure Folder's tab and folder): its outline pulls back
        INSET_GAP from them, so the bodies' walls never coincide and nothing changes height}. Indices into ``pids``."""
        key = (geo_key(g, lid), tuple(pids), round(float(S), 6))
        hit = self._touch_cache.get(key)
        if hit is not None:
            return hit
        regions = g.get("regions") or []
        rings = []
        for pid in pids:
            k = int(pid[1:]) if pid.startswith("r") and pid[1:].isdigit() else -1
            spl = (regions[k].get("splines") or []) if 0 <= k < len(regions) else []
            rings.append(heightfield.piece_rings(spl, S) if spl else [])
        sc = max(float(S), 1e-9)
        tol = TOUCH_TOL / sc
        stack, inset = [], {}
        for j in range(len(pids)):
            for i in range(j):
                rel = heightfield.rings_relation(rings[i], rings[j], tol)
                if rel == 2:
                    stack.append((i, j))
                elif rel == 1:
                    inset.setdefault(i, []).append(j)
        out = {"stack": stack, "inset": {}}
        for i, js in inset.items():
            ri = rings[i]
            for j in js:
                ri = heightfield.inset_rings(ri, rings[j], INSET_GAP / sc)
            out["inset"][i] = heightfield.rings_to_splines(ri)
        if len(self._touch_cache) > 64:
            self._touch_cache.clear()
        self._touch_cache[key] = out
        return out

    @staticmethod
    def subject_points(hulls: list, layer_z: Optional[dict] = None, canvas: bool = False) -> np.ndarray:
        """Prisms of the subject hulls (layers moved by the animation's per-layer dz). ``canvas`` (CAD iso view):
        without a visible plate the canvas square −1..1 at z = 0 joins the subject, so iso 0 continues the front
        framing; a plate's outline already spans −1..1 (its square's corners would only pad turned views)."""
        layer_z = layer_z or {}
        chunks = []
        for hull, z0, z1, lid in hulls:
            dz = float(layer_z.get(lid, 0.0)) if lid is not None else 0.0
            chunks.append(framing.prism(hull, z0 + dz, z1 + dz))
        if canvas and not any(h[3] is None for h in hulls):
            chunks.append(framing.canvas_square(0.0))
        return np.vstack(chunks) if chunks else np.zeros((0, 3))

    def plan_animation(self, project: dict, bundle: dict, appearance: str, frame_overrides: list,
                       camera: Optional[dict] = None) -> Optional[dict]:
        """One framing plan for every frame of an animation: the union of the sampled frames' subject bounds, from
        framing.plan for perspective frames, framing.ortho_plan for CAD iso frames (front view with iso > 0 in
        some frame: the iso sweep). None when every frame is the plain front view."""
        proj = norm_project(project)
        bnd = norm_bundle(bundle)
        eff = appearance_mod.resolve(proj, appearance, bnd)["project"]
        base = norm_camera(camera, eff["camera"]) if camera else dict(eff["camera"])
        hulls = self.subject_hulls(eff, bnd)
        persp, ortho = [], []
        for ov in frame_overrides:
            cam = dict(base)
            cam.update(ov.get("camera") or {})
            lz = ov.get("layerZ") or {}
            if cam.get("view", "front") == "front":
                ortho.append((self.subject_points(hulls, lz, canvas=True), _iso(cam)))
            else:
                persp.append((self.subject_points(hulls, lz), float(cam.get("tiltX", 0.0)), float(cam.get("tiltY", 0.0))))
        zoom = float(base.get("zoom", 1.0) or 1.0)
        if persp:
            return framing.plan(persp, float(base.get("fov", 30.0) or 30.0), zoom)
        if any(iso > 0.0 for _, iso in ortho):
            return framing.ortho_plan(ortho, zoom)
        return None

    # -------------------------------------------------------------------------- plate
    def _plate(self, col, plate: dict, shape: str, corner_radius: float, full_bleed: bool = False) -> bool:
        """The plate body. -> True when it is glass (transmissive: the clear / tinted-light renditions)."""
        th = max(0.0, float(plate.get("thickness", 0.16)))
        bevel = min(max(0.0, float(plate.get("bevel", 0.04))), th / 2.0)
        splines = geometry.plate_outline(shape, corner_radius)
        kp = {"plate": shape, "cr": round(corner_radius, 4)}
        data, _info = heightfield.piece_mesh(kp, splines, th, bevel, 0.0, 8, 1.0)
        ob = _mesh_object("BIS Plate", data, col, None)
        ob.location = (0.0, 0.0, -th / 2.0)
        # full-bleed masters: push the bevelled rim just outside the 2.0-wide frame (flat face edge to edge)
        grow = 1.0 + (bevel + 0.01 if full_bleed else 0.0)
        ob.scale = (grow, grow, 1.0)
        ob.color = (1.0, 1.0, 1.0, 1.0)
        preset, params = materials.resolve(plate["material"])
        paint = paint_spec(plate["fill"], None, (-1.0, -1.0, 1.0, 1.0))
        # EEVEE: the plate refracts the light probes only (raytrace=False: its frosted glass is rougher than drafts
        # trace anyway), which draws it in EEVEE's opaque layer, the one glass glyphs' screen-space refraction traces
        # see (surfaces with raytraced transmission are invisible to each other's traces): glass glyphs refract the
        # frosted plate, like Cycles, instead of the wallpaper beneath it (QA r10 N7). Cycles ignores the setting.
        spec = materials.make_spec(preset, params, paint, thickness=th, shape="plate",
                                   preview_color=paint.get("color", (0.9, 0.9, 0.9)), raytrace=False)
        mat = materials.ensure(PLATE_MATERIAL, spec)
        _set_material(ob, mat)
        self._specs.append(spec)
        ob.visible_shadow = True
        # what the light probes see of the plate: _probe
        return materials.is_transmissive(spec["params"])

    # -------------------------------------------------------------------------- layers
    def _covered_pieces(self, layers: list, geos: dict, art: dict) -> set:
        """{(layer id, region index)} of pieces completely covered (≥ 99.7 % on a 64² raster of the piece) by
        the union of opaque regions of HIGHER visible layers (normal blend, opacity 1). In the SVG they are
        invisible; through the translucent glass above they would show (e.g. as a magenta band)."""
        key = stable_hash([[L["id"], geo_key(geos[L["id"]], L["id"]) if L["id"] in geos else None, L.get("transform"),
                            L.get("opacity", 1.0), L.get("blendMode", "normal"), L.get("mode")] for L in layers] + [art])
        hit = self._cover_cache.get(key)
        if hit is not None:
            return hit
        sa, ax, ay = float(art.get("scale", 1.0)), float(art.get("x", 0.0)), float(art.get("y", 0.0))

        def canvas_rings(L, region):
            tr = L.get("transform") or {}
            sl, tx, ty = float(tr.get("scale", 1.0)), float(tr.get("x", 0.0)), float(tr.get("y", 0.0))
            out = []
            for sp in region.get("splines") or []:
                ring = geometry._flatten_ring(sp.get("points") or [], True, 4)
                if len(ring) >= 3:
                    out.append((np.asarray(ring) * sa + np.array([ax, ay])) * sl + np.array([tx, ty]))
            return out

        def opaque(L, r):
            p = r.get("paint") or {}
            return float(r.get("opacity", 1.0)) >= 0.99 and not any(
                float(st.get("opacity", 1.0)) < 0.99 for st in p.get("stops") or [])

        hidden = set()
        for i, L in enumerate(layers):
            g = geos.get(L["id"])
            if g is None or L.get("mode") == "combined":
                continue
            cover = []
            for U in layers[i + 1:]:
                gu = geos.get(U["id"])
                if gu is None or float(U.get("opacity", 1.0)) < 0.99 or U.get("blendMode", "normal") != "normal":
                    continue
                if U.get("mode") == "combined":
                    cover.append(canvas_rings(U, {"splines": gu.get("silhouette") or []}))
                else:
                    cover += [canvas_rings(U, r) for r in gu.get("regions") or [] if opaque(U, r)]
            cover = [c for c in cover if c]
            if not cover:
                continue
            for k, r in enumerate(g.get("regions") or []):
                rings = canvas_rings(L, r)
                if not rings:
                    continue
                allr = np.vstack(rings)
                (x0, y0), (x1, y1) = allr.min(axis=0), allr.max(axis=0)
                h = max(x1 - x0, y1 - y0) / 64.0
                if h <= 1e-9:
                    continue
                xs, ys = np.arange(x0 + h / 2, x1, h), np.arange(y0 + h / 2, y1, h)
                mine = geometry._scan_inside(geometry._ring_segments(rings), xs, ys)
                n = int(mine.sum())
                if n < 16:
                    continue
                left = mine.copy()
                for c in cover:
                    cx = np.vstack(c)
                    if cx[:, 0].max() < x0 or cx[:, 0].min() > x1 or cx[:, 1].max() < y0 or cx[:, 1].min() > y1:
                        continue
                    left &= ~geometry._scan_inside(geometry._ring_segments(c), xs, ys)
                    if left.sum() <= 0.003 * n:
                        hidden.add((L["id"], k))
                        break
        if len(self._cover_cache) > 32:
            self._cover_cache.clear()
        self._cover_cache[key] = hidden
        return hidden

    def _shape_material(self, Lr: dict, layer_material: dict, element: str, paint: dict, opacity: float,
                        thickness: float, env: dict, preview, mats: set) -> bpy.types.Material:
        """The single-Principled material of one shape (PLAN §11): the layer material merged with
        Layer.elementMaterials[element], named 'BIS <layer name> / <element id>'."""
        em = (Lr.get("elementMaterials") or {}).get(element) if Lr.get("glass", True) else None
        preset, params = materials.resolve(layer_material, em)
        name = materials.shape_name(Lr.get("name") or Lr["id"], element)
        if name in self._shape_names:          # (a trimmed name shared by two shapes)
            name = materials.shape_name(f"{Lr['id']} {len(self._shape_names)}", element)
        self._shape_names.add(name)
        spec = materials.make_spec(preset, params, paint, opacity=max(0.0, min(1.0, float(opacity))),
                                   mono=env.get("mono"), thickness=thickness, preview_color=preview,
                                   shape=f"{Lr['id']}/{element}")
        mat = materials.ensure(name, spec)
        self._specs.append(spec)
        mats.add(mat.name)
        return mat

    def _layer(self, col, art_ob, Lr: dict, g: dict, sa: float, env: dict, stats: dict, dz: float):
        lid = Lr["id"]
        tr = Lr["transform"]
        dp = Lr["depth"]
        sl = max(1e-4, float(tr.get("scale", 1.0)))
        S = sa * sl
        # layer empty, in Art-local units: world = sa*(T' + sl*p) + t_a  ==  (sa*p + t_a)*sl + T_l
        ta = Vector((art_ob.location.x, art_ob.location.y))
        Tl = Vector((float(tr.get("x", 0.0)), float(tr.get("y", 0.0))))
        Tp = (sl * ta + Tl - ta) / sa
        z_world = float(dp.get("z", 0.0)) + LAYER_EPS + dz          # REAL distances (no explode)
        lay = _empty(f"BIS Layer {lid}", col, art_ob)
        lay.location = (Tp.x, Tp.y, z_world / sa)
        lay.scale = (sl, sl, sl)
        names = {lay.name}
        # height-field bodies (heightfield.py): round edge radius b ≤ thickness/2 everywhere, thin parts taper
        thickness = max(0.0, float(dp.get("thickness", 0.1)))
        th_local = thickness / S
        bevel_local = min(max(0.0, float(dp.get("bevel", 0.045))), thickness / 2.0) / S
        inflate = max(0.0, min(1.0, float(dp.get("inflate", 0.0) or 0.0)))
        segments = int(dp.get("bevelSegments", 6))
        placed = []        # (object, z offset, piece index), positioned once every body's half height is known
        layer_opacity = float(Lr.get("opacity", 1.0))

        # ---- pieces ----------------------------------------------------------------------------------
        images = {}
        for im in g.get("images", []):
            if not im.get("path"):
                continue
            if os.path.isfile(im["path"]):
                images[im.get("elementId")] = im
            else:       # a missing PNG would render Cycles' magenta "missing texture" colour
                self.warnings.append(f"layer {lid}: raster image not found: {im['path']}")
        pieces = []        # (piece id, splines, z offset, opacity, rgb, raster image | None)
        # a shape with its own material (Layer.elementMaterials) needs its own body: no automatic merge then
        own = (Lr.get("elementMaterials") or {}) if Lr.get("glass", True) else {}
        combined_body = Lr.get("mode") == "combined" or (
            touching_opaque(g, images) and not any(r.get("elementId") in own for r in g["regions"]))
        if combined_body:
            # touching opaque pieces of one layer (Gmail's M + its shading wedges, DJI's facets, CRD's chevron)
            # are one body painted by the layer texture: bevelled one by one, every shared edge became a
            # V-groove that showed the plate as a white sliver with a rim highlight (QA round 3 #5)
            if Lr.get("mode") != "combined":
                stats["mergedLayers"] = stats.get("mergedLayers", 0) + 1
            pieces.append(("sil", g["silhouette"], 0.0, layer_opacity, (1.0, 1.0, 1.0), None))
        else:
            for i, r in enumerate(g["regions"]):
                zsub = float(r.get("zSub", 0.0))
                zoff = min(zsub, 40.0) * REGION_DZ if zsub >= 1.0 else zsub
                pieces.append((f"r{i}", r["splines"], zoff, float(r.get("opacity", 1.0)) * layer_opacity,
                               paint_rgb(r.get("paint") or {}), images.get(r["elementId"])))
        region_ids = {r["elementId"] for r in g["regions"]}
        cards = [im for eid, im in images.items() if eid not in region_ids]

        # ---- materials: one single-Principled material per shape (PLAN §11) ----------------------------------
        bbox = tuple(g.get("bbox") or (-1, -1, 1, 1))
        fill = Lr.get("fill") or {"type": "auto"}
        layer_mat = Lr["material"] if Lr.get("glass", True) else {"preset": "flat", "params": {}}
        eids = {f"r{i}": str(r.get("elementId") or f"r{i}") for i, r in enumerate(g["regions"])}
        rpaints = {f"r{i}": r.get("paint") for i, r in enumerate(g["regions"])}
        cast = (Lr.get("shadow") or {}).get("kind", "physical") != "none"      # real shadows only: on / off
        mats: set = set()

        key_base = {"h": geo_key(g, lid), "lid": lid}
        n_open = sum(geometry.count_open(p[1] or []) for p in pieces)
        if n_open:
            # fills are always closed (an SVG fill closes an open subpath); a flag left open used to sweep the
            # round bevel along the outline as a hollow tube
            log(f"layer {lid}: {n_open} open spline(s) treated as closed fills")
            stats["openSplines"] = stats.get("openSplines", 0) + n_open
        # pieces of one layer that overlap / touch (_relations): touching ones pull back from each other, overlapping
        # ones are stacked by their REAL heights in paint order, so bodies of one layer never interpenetrate
        rel = (self._relations(g, lid, [pc[0] for pc in pieces], S) if len(pieces) > 1 and not combined_body
               else {"stack": [], "inset": {}})
        for n_piece, (pid, splines, zoff, op, rgb, raster) in enumerate(pieces):
            if not splines:
                continue
            kp = {**key_base, "p": pid, "n": len(splines)}
            if n_piece in rel["inset"]:
                splines = rel["inset"][n_piece]
                kp["inset"] = INSET_GAP
                stats["insetPieces"] = stats.get("insetPieces", 0) + 1
                if not splines:
                    # a sliver no wider than 2 × INSET_GAP along the piece it touches: nothing of it is left once it
                    # pulls back (kept, it cut into that piece); see heightfield.inset_rings
                    log(f"layer {lid} {pid}: sliver along a touching piece dropped")
                    stats["insetSlivers"] = stats.get("insetSlivers", 0) + 1
                    continue
            data, _hinfo = heightfield.piece_mesh(kp, splines, th_local, bevel_local, inflate, segments, S)
            if data is None:
                self.warnings.append(f"layer {lid} {pid}: degenerate outline skipped")
                continue
            if raster is not None and fill.get("type", "auto") == "auto":
                # raster element extruded along its alpha contour: project its own PNG with its exact
                # placement (its alpha is honoured: traced contours may include soft, partly transparent pixels)
                ppaint = {"kind": "texture", "image": raster["path"], "uv": image_uv(raster, bbox), "has_alpha": True}
            else:
                ppaint = paint_spec(fill, g.get("texturePath"), bbox, rpaints.get(pid))
            pmat = self._shape_material(Lr, layer_mat, eids.get(pid, "body"), ppaint, op, th_local, env, rgb, mats)
            ob = _mesh_object(f"BIS {lid} {pid}", data, col, lay)
            placed.append((ob, zoff, n_piece))
            ob.scale = (1.0, 1.0, 1.0)
            ob.color = (*rgb, max(0.0, min(1.0, op)))
            covered = pid.startswith("r") and (lid, int(pid[1:])) in self._hidden
            if ob.visible_transmission == covered:
                ob.visible_transmission = not covered
                ob.visible_glossy = not covered
            if covered:
                stats["hiddenPieces"] = stats.get("hiddenPieces", 0) + 1
            _set_material(ob, pmat)
            if ob.visible_shadow != cast:
                ob.visible_shadow = cast
            names.add(ob.name)                  # (what the light probes see of it: _probe)
            stats["pieces"] += 1
        # every body of the layer shares one mid-plane at z + thickness/2; an inflated layer is lifted so that its
        # lowest point stays on z (the dome is mirrored below the mid-plane)
        halves = [float(ob.data.get("bis_half", 0.0)) for ob, _z, _n in placed]
        lift = max(0.0, max(halves + [0.0]) - th_local / 2.0)
        base = [(thickness / 2.0 + zoff) / S + lift for _ob, zoff, _n in placed]
        shifts = [0.0] * len(placed)
        if rel["stack"]:
            at = {n: k for k, (_o, _z, n) in enumerate(placed)}
            pairs = [(at[i], at[j]) for i, j in rel["stack"] if i in at and j in at]
            shifts = list(heightfield.stack_shifts(len(placed), pairs, halves, STACK_GAP / S, base))
            stats["stackedPieces"] = stats.get("stackedPieces", 0) + sum(1 for x in shifts if x > 0)
        for (ob, _z, _n), b0, sh in zip(placed, base, shifts):
            ob.location = (0.0, 0.0, b0 + float(sh))

        for k, im in enumerate(cards):
            bb = im.get("bbox") or bbox
            quad = image_quad(im, bbox)
            ckp = {"card": im.get("path"), "quad": [list(p) for p in quad]}
            cspl = [geometry.poly_spline(quad)]
            data, _ = heightfield.piece_mesh(ckp, cspl, CARD_THICKNESS / S, 0.0, 0.0, 1, S)
            if data is None:
                continue
            ob = _mesh_object(f"BIS {lid} img{k}", data, col, lay)
            ob.location = (0.0, 0.0, (CARD_THICKNESS / 2.0 + 0.0005) / S)
            ob.color = (1.0, 1.0, 1.0, max(0.0, min(1.0, float(im.get("opacity", 1.0)) * layer_opacity)))
            cpaint = {"kind": "texture", "image": im["path"], "uv": image_uv(im, bbox), "has_alpha": True}
            # an unextruded <image> is flat art: the 'flat' preset (emission-painted) unless the shape has its own
            cmat = self._shape_material(Lr, {"preset": "flat", "params": {}}, str(im.get("elementId") or f"img{k}"),
                                        cpaint, ob.color[3], CARD_THICKNESS / S, env, (1.0, 1.0, 1.0), mats)
            _set_material(ob, cmat)
            if ob.visible_shadow != cast:
                ob.visible_shadow = cast
            names.add(ob.name)
        return names, mats

    # -------------------------------------------------------------------------- wallpaper
    def _wallpaper(self, col, kind: str, plate_thickness: float, camera_visible: bool) -> None:
        ob = bpy.data.objects.get("BIS Wallpaper")
        if ob is None or ob.type != "MESH":
            me = bpy.data.meshes.new("BIS Wallpaper")
            s = 30.0
            me.from_pydata([(-s, -s, 0), (s, -s, 0), (s, s, 0), (-s, s, 0)], [], [(0, 1, 2, 3)])
            ob = bpy.data.objects.new("BIS Wallpaper", me)
        if ob.name not in col.objects:
            col.objects.link(ob)
        ob.location = (0.0, 0.0, -plate_thickness - PLATE_WALLPAPER_GAP)
        ob.visible_camera = camera_visible
        ob.visible_shadow = False
        ob.visible_diffuse = False
        ob.visible_glossy = True
        ob.visible_transmission = True
        w = appearance_mod.wallpaper(kind)
        mat = bpy.data.materials.get(WALLPAPER_MATERIAL) or bpy.data.materials.new(WALLPAPER_MATERIAL)
        from .nodes import Graph, TopologyMismatch, layout
        key = "wp2"

        def build(gr: Graph):
            tc = gr.node("ShaderNodeTexCoord")
            pos = tc.outputs["Object"]
            y = gr.separate(pos).outputs["Y"]
            A = appearance_mod
            t = gr.map_range(y, A.WP_Y, -A.WP_Y, 0.0, 1.0)
            base = gr.mix_rgb(t, (*hex_to_linear(w["top"]), 1), (*hex_to_linear(w["bottom"]), 1))
            for b in w["blobs"]:
                d = gr.vmath("DISTANCE", pos, (b["x"] * A.WP_POS, b["y"] * A.WP_POS, 0.0))
                f = gr.map_range(d, 0.0, b["r"] * A.WP_R, 1.0, 0.0, interp="SMOOTHSTEP")
                base = gr.mix_rgb(gr.math("MULTIPLY", f, A.WP_MIX), base, (*hex_to_linear(b["color"]), 1))
            p = gr.node("ShaderNodeBsdfPrincipled", "Principled BSDF")
            gr.inputs(p, {"Base Color": (0.0, 0.0, 0.0, 1.0), "Roughness": 1.0, "Specular IOR Level": 0.0,
                          "Emission Color": base, "Emission Strength": 1.0})
            out = gr.node("ShaderNodeOutputMaterial", "Material Output", target="ALL")
            gr.link(p.outputs[0], out.inputs["Surface"])

        if mat.get("bis_key") == key:
            try:
                build(Graph(mat.node_tree, update=True))
            except TopologyMismatch:
                mat["bis_key"] = ""
        if mat.get("bis_key") != key:
            mat.node_tree.nodes.clear()
            build(Graph(mat.node_tree))
            mat["bis_key"] = key
            layout(mat.node_tree)
        if len(ob.data.materials) == 0:
            ob.data.materials.append(mat)
        else:
            ob.data.materials[0] = mat

    # -------------------------------------------------------------------------- EEVEE plate probe
    @staticmethod
    def _sphere_probe(name: str, col, want: bool) -> Optional[bpy.types.Object]:
        """The sphere light probe object ``name`` (created / linked when ``want``, else removed)."""
        ob = bpy.data.objects.get(name)
        if ob is not None and (not want or ob.type != "LIGHT_PROBE"):
            bpy.data.objects.remove(ob, do_unlink=True)
            ob = None
        if not want:
            return None
        if ob is None:
            lp = bpy.data.lightprobes.get(name)
            if lp is None or lp.type != "SPHERE":
                lp = bpy.data.lightprobes.new(name, "SPHERE")
            ob = bpy.data.objects.new(name, lp)
        if ob.name not in col.objects:
            col.objects.link(ob)
        return ob

    @staticmethod
    def _set_probe(ob, settings: dict, location: tuple, scale: tuple = (1.0, 1.0, 1.0)) -> None:
        """Assign only on change (every write re-captures the probe)."""
        lp = ob.data
        for k, v in settings.items():
            cur = getattr(lp, k)
            if (abs(cur - v) > 1e-6) if isinstance(v, float) else cur != v:
                setattr(lp, k, v)
        if (Vector(ob.location) - Vector(location)).length > 1e-7:
            ob.location = location
        if (Vector(ob.scale) - Vector(scale)).length > 1e-7:
            ob.scale = scale

    def _probe(self, col, eff: dict, bnd: dict, sa: float, plate_ok: bool, glass_plate: bool = False,
               layer_z: Optional[dict] = None, plate: Optional[dict] = None, wp_kind: Optional[str] = None,
               bodies: Optional[list] = None) -> None:
        """EEVEE sphere light probes (Cycles ignores light probes; QA r9 N1 / N2, QA r10 N7). Scene-level only: no
        material reads a probe on purpose; they are what EEVEE's refraction falls back to.

        EEVEE refracts by screen-space ray tracing; a ray that finds nothing behind the glass on screen (glass floating
        above the plate in iso / perspective views, glass beyond the plate's silhouette), every alpha-BLENDED surface
        (translucent glass cannot trace) and every surface rougher than the draft's trace_max_roughness falls back to the
        light probes. Without a probe that was the studio world behind the icon, which is dark: Photos' floating petals at
        iso read (76,65,50) against Cycles' (155,126,85), and a 66 % Contacts head rendered near-black.

        ``BIS Probe`` (the plate probe) captures the scene from just above the plate's front face with the layer bodies
        hidden from it (Object.hide_probe_sphere: their undersides would cover the plate): its lower hemisphere is the lit
        plate (and the wallpaper / world beyond its edge), what Cycles' rays reach through the glass. A GLASS plate
        (clear / tinted-light renditions: frosted glass over the wallpaper) must not capture itself: the frosted plate
        refracted a flat grey copy of itself (round-8 review); it refracts the wallpaper beneath it. What the glyphs above
        it should read differs with the wallpaper; in Cycles a frosted glyph is
        * over a LIGHT wallpaper: the wallpaper seen through the plate. The plate is hidden from the probe (which shows
          the wallpaper), and drafts trace frosted glyphs against the plate on screen (``traceMaxRoughness``; the plate
          is drawn in EEVEE's opaque layer for that, :meth:`_plate`).
        * over a DARK wallpaper: studio light scattered inside the frosted glass (Cycles keeps ~98 % of a clear-dark
          glyph's brightness with the wallpaper switched off); reading the dark wallpaper straight through the plate,
          the glyphs rendered near-black ((32,35,46) vs Cycles' (70,71,79), QA r10 N7). The plate probe moves to the
          plate's middle with its near clip PLATE_PROBE_CLIP past the plate's faces (it still sees the wallpaper and the
          studio, not the plate), and ``BIS Glyph Probe`` captures the space above the plate WITH the studio-lit plate
          and the glass bodies themselves (glass seen through glass, as in Cycles: clear-dark glyph L* 5.7 below Cycles
          instead of 8.2 with them hidden, Ti84 8.8 instead of 15.7); its BOX influence holds the layer bodies above
          GLYPH_PROBE_FLOOR (EEVEE picks the smallest influence first), so glass glyphs read the lit frosted plate and
          glass, the plate the wallpaper. Flat cards stay below the floor with the plate: in the glyph probe a flat
          soft-alpha card refracted the single-sample capture of the bodies' transparent shadows on the plate (Vanced
          Neon's glow halo, dashed).
        The viewport draws the studio behind the icon in its transmission pass instead. Limits: a capture is a single
        EEVEE sample with the bodies' (transparent) shadows on the plate in it, noisy, so flat glass that would read it
        through a large magnification stays raytraced (materials.art_alpha_is_soft); EEVEE's frosted glass has no
        multiple scattering, so clear-dark glyphs stay darker than Cycles' milky ones.
        The plate probe exists whenever something reads it: layer bodies over a plate, or a GLASS plate on its own (QA r11
        N11: an icon with no visible layer - Template, or every layer hidden - lost the probe, and its frosted plate
        refracted the dark studio world: clear-light L* 35 vs Cycles' 91). The glyph probe only with layers."""
        layers = [Lr for Lr in eff["layers"] if Lr.get("visible", True) and Lr["id"] in bnd["layers"]]
        want = bool(plate_ok and (layers or glass_plate))
        dark_glass = bool(glass_plate and wp_kind == "dark")
        pob = bpy.data.objects.get("BIS Plate")
        hide = bool(glass_plate and not dark_glass)
        if pob is not None and pob.hide_probe_sphere != hide:
            pob.hide_probe_sphere = hide
        for bob in bodies or []:
            if bob.hide_probe_sphere == dark_glass:
                bob.hide_probe_sphere = not dark_glass
        ob = self._sphere_probe(PROBE_NAME, col, want)
        gob = self._sphere_probe(GLYPH_PROBE_NAME, col, bool(want and dark_glass and layers))
        if not want:
            return
        layer_z = layer_z or {}
        top, ext = 0.0, 1.0
        for hull, z0, z1, lid in self.subject_hulls(eff, bnd):
            if lid is None:
                continue
            top = max(top, z1 + float(layer_z.get(lid, 0.0)))
            if len(hull):
                ext = max(ext, float(np.abs(hull).max()))
        reach = {"influence_type": "ELIPSOID", "influence_distance": max(1.5, top) + PROBE_REACH, "falloff": 0.2,
                 "clip_end": 100.0}
        if not dark_glass:
            self._set_probe(ob, {**reach, "clip_start": 0.002}, (0.0, 0.0, PROBE_Z))
            return
        th = max(0.0, float((plate or {}).get("thickness", 0.16)))
        self._set_probe(ob, {**reach, "clip_start": th / 2.0 + PLATE_PROBE_CLIP}, (0.0, 0.0, -th / 2.0))
        if gob is None:   # no layer bodies: nothing reads a glyph probe
            return
        # the glyph probe: a box from GLYPH_PROBE_FLOOR to above the highest body; captured from its centre (unit
        # influence distance: the box's size is the object's scale)
        z0, z1 = GLYPH_PROBE_FLOOR, max(top, GLYPH_PROBE_FLOOR) + GLYPH_PROBE_MARGIN
        half = (z1 - z0) / 2.0
        xy = ext + GLYPH_PROBE_MARGIN
        self._set_probe(gob, {"influence_type": "BOX", "influence_distance": 1.0, "falloff": GLYPH_PROBE_FALLOFF,
                              "clip_start": 0.002, "clip_end": 100.0}, (0.0, 0.0, z0 + half), (xy, xy, half))

    def specs(self) -> list:
        return list(self._specs)

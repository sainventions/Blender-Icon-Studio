"""Scene construction: (project, geometry bundle, appearance) -> Blender scene, incrementally.

Layout (PLAN §3, D1): icon in the XY plane, camera on +Z looking −Z, 1 BU = 1 art unit.

    BIS Icon (collection)
      BIS Plate                       canvas space; back at −thickness, front face at z = 0
      BIS Art          (empty)        canvas.art: uniform scale + (x, y)
        BIS Layer <id> (empty)        layer.transform (scale about the canvas origin, then translate)
                                      + z = depth.z · camera.explode + ε  (expressed in Art-local units)
          BIS <id> r<i> / sil / img<k>  baked mesh objects (geometry.solid_mesh); object coords == art coords
    BIS Rig (collection)              camera, 4 area lights, wallpaper plane

Everything is updated in place between renders: objects/empties are reused by name, piece meshes (baked
once from the bevelled curve / GN stack) come from geometry's cache (layer hash + depth params), materials
update their node values in place.
"""
from __future__ import annotations

import math
import os
from typing import Optional

import bpy
import numpy as np
from mathutils import Matrix, Vector

from . import appearance as appearance_mod
from . import framing, geometry, lighting, materials
from .defaults import norm_bundle, norm_camera, norm_project
from .gpu import configure_scene
from .util import gradient_samples, hex_to_linear, hex_to_srgb, log, srgb_to_linear, stable_hash

LAYER_EPS = 0.002          # layer back face above the plate front face
REGION_DZ = 0.001          # zSub step (zSub is interpreted as a sub-layer index)
CARD_THICKNESS = 0.004
PLATE_WALLPAPER_GAP = 0.9  # wallpaper plane below the plate's back face


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
    """Object showing a baked piece mesh (geometry.solid_mesh). No modifiers: nothing is re-evaluated
    per render."""
    ob = bpy.data.objects.get(name)
    if ob is not None and ob.type != "MESH":
        bpy.data.objects.remove(ob)
        ob = None
    if ob is None:
        ob = bpy.data.objects.new(name, data)
    elif ob.data != data:
        old = ob.data
        ob.data = data
        if old is not None and old.users == 0 and not geometry.is_cached_mesh(old):
            bpy.data.meshes.remove(old)
    for m in list(ob.modifiers):
        ob.modifiers.remove(m)
    if ob.name not in col.objects:
        col.objects.link(ob)
    if ob.parent != parent:
        ob.parent = parent
        ob.matrix_parent_inverse = Matrix.Identity(4)
    return ob


def _curve_object(name: str, cu: bpy.types.Curve, col: bpy.types.Collection,
                  parent: Optional[bpy.types.Object]) -> bpy.types.Object:
    """Editable piece (saved .blend files): the cached curve datablock itself — live extrude / round bevel /
    offset on the curve route; the GN route adds live Fill Curve → Solidify → Bevel modifiers."""
    ob = bpy.data.objects.get(name)
    if ob is not None and (ob.type != "CURVE" or ob.data != cu):
        bpy.data.objects.remove(ob, do_unlink=True)
        ob = None
    if ob is None:
        ob = bpy.data.objects.new(name, cu)
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


def _bbox_intersects(a, b) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


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
def paint_spec(fill: dict, texture: Optional[str], bbox=(-1.0, -1.0, 1.0, 1.0)) -> dict:
    """Layer/plate fill -> material paint spec (object coordinates of the target object)."""
    t = (fill or {}).get("type", "auto")
    if t == "auto":
        if texture and os.path.isfile(texture):
            return {"kind": "texture", "image": texture}
        return {"kind": "object"}
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


HALO_SOFT_MIN = 0.25         # share of the visible pixels that are soft (0.02 < alpha < 0.6)
GLOW_SOFT_MIN = 0.5          # ... above this a raster region IS a glow (light, not an object): card only
_SOFT_ALPHA: dict = {}


def soft_alpha_fraction(path: str) -> float:
    """Share of an image's visible pixels (alpha > 0.02) that are only partly opaque (alpha < 0.6) around a
    solid core: glow halos that an alpha-traced extrusion drops (0 without a solid core). Cached by path +
    mtime."""
    try:
        key = (os.path.normcase(os.path.abspath(path)), os.path.getmtime(path))
    except OSError:
        return 0.0
    hit = _SOFT_ALPHA.get(key)
    if hit is not None:
        return hit
    frac = 0.0
    img = materials.load_image(path)
    if img is not None and img.size[0] * img.size[1] > 0:
        px = np.empty(img.size[0] * img.size[1] * 4, dtype=np.float32)
        img.pixels.foreach_get(px)
        a = px[3::4]
        vis = int((a > 0.02).sum())
        frac = float(((a > 0.02) & (a < 0.6)).sum()) / vis if vis else 0.0
        if vis and float((a >= 0.6).sum()) / vis < 0.1:
            frac = 0.0          # no solid core (a translucent shading overlay, e.g. Find Device): no halo
    _SOFT_ALPHA[key] = frac
    return frac


def touching_opaque(g: dict, images: Optional[dict] = None) -> bool:
    """True when some regions of a layer share edges (the union silhouette has fewer outer contours than the
    regions together) and every region is opaque vector paint — the layer then renders as one body."""
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

    # -------------------------------------------------------------------------- setup
    @property
    def scene(self) -> bpy.types.Scene:
        return bpy.context.scene

    def reset(self) -> None:
        """Empty file (keeps preferences: OptiX) + fresh collections."""
        bpy.ops.wm.read_homefile(use_empty=True)
        configure_scene(self.scene)
        geometry.reset_caches()
        self.info = {}

    def collections(self):
        icon = _collection("BIS Icon")
        rig = _collection("BIS Rig")
        return icon, rig

    # -------------------------------------------------------------------------- main entry
    def build(self, project: dict, bundle: dict, appearance: str = "light", *, camera: Optional[dict] = None,
              full_bleed: bool = False, backdrop: Optional[str] = None, backdrop_color: Optional[str] = None,
              overrides: Optional[dict] = None, editable: bool = False, engine: Optional[str] = None) -> dict:
        """Build/update the scene. ``overrides`` (animations): {'lightAngle', 'explode', 'layerZ': {id: dz}}.
        ``editable`` (save_blend): pieces become live curve objects (round bevel / GN modifier stack) instead
        of the baked meshes renders use, so the .blend can be tweaked in Blender. ``engine`` (render engine
        id of the tier about to render) selects the per-engine light calibration (lighting.ENGINE_CAL)."""
        self.warnings = []
        self.editable = bool(editable)
        overrides = overrides or {}
        proj = norm_project(project)
        bnd = norm_bundle(bundle)
        res = appearance_mod.resolve(proj, appearance, bnd)
        eff, env = res["project"], res["env"]
        cam = norm_camera(camera, eff["camera"]) if camera else dict(eff["camera"])
        if "explode" in overrides:
            cam["explode"] = overrides["explode"]
        if "camera" in overrides:
            cam.update(overrides["camera"])
        lighting_spec = dict(eff["lighting"])
        rset = eff["render"]
        backdrop = backdrop or rset.get("backdrop", "transparent")
        backdrop_color = backdrop_color or rset.get("backdropColor", "#1c1c22")
        # paints are pre-compensated for the view transform of the colour mode (materials.display_paint)
        self._cm = str(rset.get("colorMode", "neutral") or "neutral")

        icon_col, rig_col = self.collections()
        scene = self.scene
        self._specs = []
        rig = lighting.resolve(lighting_spec, env["envScale"], env["keyScale"], overrides.get("lightAngle"))
        L = lighting.key_vector(rig)
        self._lit = max(0.4, min(1.4, 0.55 + 0.45 * float(rig["key"])))
        lighting.update_lights(scene, rig_col, rig, engine)
        lighting.update_world(scene, rig, (*hex_to_linear(backdrop_color), 1.0), engine)
        # perspective views are auto-framed on the subject (framing.py); animations pass one shared plan
        fplan = None
        if cam.get("view", "front") != "front" and not full_bleed:
            fplan = overrides.get("framing")
            if not fplan:
                hulls = self.subject_hulls(eff, bnd, full_bleed)
                pts = self.subject_points(hulls, float(cam.get("explode", 1.0)), overrides.get("layerZ") or {})
                fplan = framing.plan([(pts, float(cam.get("tiltX", 0.0)), float(cam.get("tiltY", 0.0)))],
                                     float(cam.get("fov", 30.0) or 30.0), float(cam.get("zoom", 1.0) or 1.0))
        self._camera(rig_col, cam, full_bleed, fplan)

        canvas = eff["canvas"]
        shape = "square" if full_bleed else canvas["shape"]
        keep: set[str] = set()
        used_mats: set[str] = set()

        # ---- EEVEE roles: glass refracts only when no glass layer above overlaps it (D7) -------------
        layers = [Lr for Lr in eff["layers"] if Lr.get("visible", True)]
        geo_by_id = bnd["layers"]
        boxes = {}
        for Lr in layers:
            g = geo_by_id.get(Lr["id"])
            if g is None:
                continue
            boxes[Lr["id"]] = self._canvas_bbox(g["bbox"], canvas["art"], Lr["transform"])
        presets_eff = {Lr["id"]: ("flat" if not Lr.get("glass", True) else Lr["material"]["preset"]) for Lr in layers}
        roles = {}
        glass_ids = [Lr["id"] for Lr in layers if Lr["id"] in boxes and materials.is_glass(presets_eff[Lr["id"]])]
        by_id = {Lr["id"]: Lr for Lr in layers}
        for i, lid in enumerate(glass_ids):
            above = [o for o in glass_ids[i + 1:] if _bbox_intersects(boxes[lid], boxes[o])]
            roles[lid] = "fake" if any(self._overlaps(by_id[lid], by_id[o], geo_by_id, canvas["art"])
                                       for o in above) else "refract"

        # ---- pieces the SVG hides completely under opaque higher art (Translate's magenta shadow-caster under
        # the blue card): glass above must not reveal them (they still cast shadows / show their sides) --------
        self._hidden = self._covered_pieces(layers, geo_by_id, canvas["art"])

        # ---- plate ---------------------------------------------------------------------------------
        plate = canvas["plate"]
        plate_ok = plate.get("visible", True) and shape != "none" and (plate["fill"].get("type") != "none")
        wp_kind = env.get("wallpaper") or (("dark" if env.get("dark") else "light") if backdrop == "wallpaper" else None)
        if plate_ok:
            pp = plate["material"]["preset"]
            backdrop_wp = None
            if materials.is_glass(pp) and wp_kind:
                # EEVEE: a frosted glass plate over the wallpaper (clear / tinted renditions) cannot refract a
                # camera-invisible wallpaper (screen-space tracing falls back to the dark studio world). The
                # draft shades it as frosted glass over the *known* wallpaper instead (no raytraced refraction),
                # which also puts the plate into the depth buffer so the glass layers above refract it.
                # Cycles stays physically exact.
                backdrop_wp = appearance_mod.wallpaper_linear(wp_kind, PLATE_WALLPAPER_GAP + plate["thickness"])
                role = "backdrop"
            else:
                role = "refract" if materials.is_glass(pp) else "opaque"
            self._plate(icon_col, plate, shape, canvas.get("cornerRadius", 0.225), role, L, env, full_bleed,
                        backdrop_wp)
            keep.add("BIS Plate")
            used_mats.add("BIS Mat Plate")

        # ---- art root --------------------------------------------------------------------------------
        art = canvas["art"]
        sa = max(1e-4, float(art.get("scale", 1.0)))
        art_ob = _empty("BIS Art", icon_col)
        art_ob.location = (float(art.get("x", 0.0)), float(art.get("y", 0.0)), 0.0)
        art_ob.scale = (sa, sa, sa)
        art_ob.rotation_euler = (0.0, 0.0, 0.0)
        keep.add(art_ob.name)

        explode = float(cam.get("explode", 1.0))
        stats = {"layers": 0, "pieces": 0, "gnFallback": 0, "clampedBevel": 0}
        layer_z = overrides.get("layerZ") or {}
        for Lr in layers:
            g = geo_by_id.get(Lr["id"])
            if g is None:
                self.warnings.append(f"layer {Lr['id']} has no geometry in the bundle")
                continue
            names, mats = self._layer(icon_col, art_ob, Lr, g, sa, explode, roles.get(Lr["id"], "opaque"),
                                      presets_eff[Lr["id"]], L, env, stats, float(layer_z.get(Lr["id"], 0.0)))
            keep |= names
            used_mats |= mats
            stats["layers"] += 1

        # ---- wallpaper ---------------------------------------------------------------------------------
        if wp_kind:
            self._wallpaper(rig_col, wp_kind, plate["thickness"], camera_visible=(backdrop == "wallpaper"))
            keep.add("BIS Wallpaper")
            used_mats.add("BIS Mat Wallpaper")

        # ---- cleanup stale BIS objects / materials ---------------------------------------------------
        for ob in list(icon_col.objects) + [o for o in rig_col.objects if o.name == "BIS Wallpaper"]:
            if ob.name not in keep:
                data = ob.data if ob.type == "MESH" else None
                bpy.data.objects.remove(ob, do_unlink=True)
                if data is not None and data.users == 0 and not geometry.is_cached_mesh(data):
                    bpy.data.meshes.remove(data)
        for m in list(bpy.data.materials):
            if m.name.startswith("BIS Mat") and m.name not in used_mats and m.users == 0:
                bpy.data.materials.remove(m)
        geometry.purge_unused_curves()
        geometry.purge_unused_meshes()
        materials.purge_unused_images()

        # ---- scene-level flags ---------------------------------------------------------------------------
        all_specs = self._specs
        neon = [s for s in all_specs if s["preset"] == "neon"]
        self.info = {
            "appearance": res["appearance"],
            "backdrop": backdrop,
            "backdropColor": backdrop_color,
            "wallpaper": appearance_mod.wallpaper(wp_kind) if wp_kind else None,
            "maxFrost": max([materials.max_frost(s) for s in all_specs] + [0.0]),
            "volume": any(materials.uses_volume(s) for s in all_specs),
            "neonBloom": max([float(s["params"].get("bloom", 0.6)) for s in neon] + [0.0]) if neon else 0.0,
            "fullBleed": bool(full_bleed),
            "camera": cam,
            "light": {"angle": rig["angle"], "elevation": rig["elevation"]},
            "stats": stats,
            "warnings": list(self.warnings),
        }
        return self.info

    # -------------------------------------------------------------------------- camera
    def _camera(self, col: bpy.types.Collection, cam: dict, full_bleed: bool, fplan: Optional[dict] = None) -> None:
        ob = bpy.data.objects.get("BIS Camera")
        if ob is None:
            ob = bpy.data.objects.new("BIS Camera", bpy.data.cameras.new("BIS Camera"))
        if ob.name not in col.objects:
            col.objects.link(ob)
        cd = ob.data
        zoom = max(0.05, float(cam.get("zoom", 1.0)))
        cd.clip_start = 0.05
        cd.clip_end = 200.0
        if cam.get("view", "front") == "front" or full_bleed or not fplan:
            # front: the fixed App-Store framing (room for shadows); unchanged by auto-framing
            cd.type = "ORTHO"
            cd.ortho_scale = (2.0 if full_bleed else 2.24) / zoom
            cd.shift_x = cd.shift_y = 0.0
            ob.matrix_world = Matrix.Translation((0.0, 0.0, 10.0))
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

    # -------------------------------------------------------------------------- framing subject
    def subject_hulls(self, eff: dict, bnd: dict, full_bleed: bool = False) -> list:
        """Convex hulls (canvas XY) of everything visible + their z spans, for perspective auto-framing:
        [(hull (M, 2), z_base, thickness, explode_scaled, layer id | None)]. The plate spans −thickness..0;
        a layer spans z·explode + ε + dz .. + thickness."""
        canvas = eff["canvas"]
        shape = "square" if full_bleed else canvas["shape"]
        out = []
        plate = canvas["plate"]
        if plate.get("visible", True) and shape != "none" and (plate["fill"].get("type") != "none"):
            th = max(0.0, float(plate.get("thickness", 0.16)))
            ring = [p for s in geometry.plate_outline(shape, canvas.get("cornerRadius", 0.225))
                    for p in geometry._flatten_ring(s["points"], True, 6)]
            out.append((framing.hull2d(np.asarray(ring)), -th, th, False, None))
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
            dp = Lr["depth"]
            out.append((framing.hull2d(pts), float(dp.get("z", 0.0)), max(0.0, float(dp.get("thickness", 0.1))),
                        True, Lr["id"]))
        return out

    @staticmethod
    def subject_points(hulls: list, explode: float, layer_z: Optional[dict] = None) -> np.ndarray:
        layer_z = layer_z or {}
        chunks = []
        for hull, z, th, scaled, lid in hulls:
            z0 = (z * explode + LAYER_EPS + float(layer_z.get(lid, 0.0))) if scaled else z
            chunks.append(framing.prism(hull, z0, z0 + th))
        return np.vstack(chunks) if chunks else np.zeros((0, 3))

    def plan_animation(self, project: dict, bundle: dict, appearance: str, frame_overrides: list,
                       camera: Optional[dict] = None) -> Optional[dict]:
        """One framing plan (framing.plan) for every perspective frame of an animation: the union of the
        sampled frames' subject bounds. None when no frame is perspective."""
        proj = norm_project(project)
        bnd = norm_bundle(bundle)
        eff = appearance_mod.resolve(proj, appearance, bnd)["project"]
        base = norm_camera(camera, eff["camera"]) if camera else dict(eff["camera"])
        hulls = None
        frames = []
        for ov in frame_overrides:
            cam = dict(base)
            cam.update(ov.get("camera") or {})
            if "explode" in ov:
                cam["explode"] = ov["explode"]
            if cam.get("view", "front") == "front":
                continue
            if hulls is None:
                hulls = self.subject_hulls(eff, bnd)
            frames.append((self.subject_points(hulls, float(cam.get("explode", 1.0)), ov.get("layerZ") or {}),
                           float(cam.get("tiltX", 0.0)), float(cam.get("tiltY", 0.0))))
        if not frames:
            return None
        return framing.plan(frames, float(base.get("fov", 30.0) or 30.0), float(base.get("zoom", 1.0) or 1.0))

    # -------------------------------------------------------------------------- plate
    def _plate(self, col, plate: dict, shape: str, corner_radius: float, role: str, L, env: dict,
               full_bleed: bool = False, backdrop_wp: Optional[dict] = None) -> None:
        th = max(0.0, float(plate.get("thickness", 0.16)))
        bevel_req = max(0.0, float(plate.get("bevel", 0.04)))
        bevel, route = geometry.effective_bevel(bevel_req, th, 1.0)
        splines = geometry.plate_outline(shape, corner_radius)
        kp = {"plate": shape, "cr": round(corner_radius, 4)}
        data, route = geometry.solid_mesh(kp, splines, th, bevel, route, 8, gn_bevel=bevel_req)
        if self.editable:
            cu, route = geometry.curve_data(kp, splines, th, bevel, route, 8, bevel_req)
            ob = _curve_object("BIS Plate", cu, col, None)
        else:
            ob = _mesh_object("BIS Plate", data, col, None)
        ob.location = (0.0, 0.0, -th / 2.0)
        # full-bleed masters: push the bevelled rim just outside the 2.0-wide frame (flat face edge to edge)
        grow = 1.0 + (bevel + 0.01 if full_bleed else 0.0)
        ob.scale = (grow, grow, 1.0)
        ob.color = (1.0, 1.0, 1.0, 1.0)
        pm = plate["material"]
        paint = paint_spec(plate["fill"], None, (-1.0, -1.0, 1.0, 1.0))
        spec = materials.make_spec(
            pm.get("preset", "satin"), pm.get("params"), paint,
            mono=None, clear=False, alpha=bool(paint.get("has_alpha")),
            shadow={"kind": "none", "opacity": 0.0}, role=role, thickness=th, light=L,
            bbox=(-1.0, -1.0, 1.0, 1.0), inflate=0.0, emission=0.0,
            preview_color=paint.get("color", (0.9, 0.9, 0.9)), eevee_backdrop=backdrop_wp, obj_scale=grow,
            cm=getattr(self, "_cm", "neutral"), plate=True,
        )
        mat = materials.ensure("BIS Mat Plate", spec)
        _set_material(ob, mat)
        if self.editable:
            geometry.apply_route(ob, route, th, float(cu.get("bis_gb", 0.0) or 0.0), 8, mat)
        self._specs.append(spec)
        ob.visible_shadow = True

    # -------------------------------------------------------------------------- layers
    @staticmethod
    def _overlaps(a: dict, b: dict, geos: dict, art: dict) -> bool:
        """Silhouette overlap of two layers in canvas space (coarse raster, 1-cell dilation)."""
        def mask(L):
            g = geos[L["id"]]
            sa, sl = float(art.get("scale", 1)), float(L["transform"].get("scale", 1))
            dx = float(art.get("x", 0)) * sl + float(L["transform"].get("x", 0))
            dy = float(art.get("y", 0)) * sl + float(L["transform"].get("y", 0))
            sil = g["silhouette"] or [s for r in g["regions"] for s in r["splines"]]
            return geometry.occupancy(geo_key(g, L["id"]), sil, sa * sl, dx, dy)
        try:
            return bool((mask(a) & mask(b)).any())
        except Exception as ex:  # never let a heuristic break a render
            log("overlap test failed:", ex)
            return True

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

    @staticmethod
    def _canvas_bbox(bbox, art: dict, tr: dict):
        sa, ax, ay = float(art.get("scale", 1)), float(art.get("x", 0)), float(art.get("y", 0))
        sl, tx, ty = float(tr.get("scale", 1)), float(tr.get("x", 0)), float(tr.get("y", 0))
        f = lambda x, y: ((x * sa + ax) * sl + tx, (y * sa + ay) * sl + ty)  # noqa: E731
        x0, y0 = f(bbox[0], bbox[1])
        x1, y1 = f(bbox[2], bbox[3])
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    def _layer(self, col, art_ob, Lr: dict, g: dict, sa: float, explode: float, role: str, preset: str,
               L, env: dict, stats: dict, dz: float):
        lid = Lr["id"]
        tr = Lr["transform"]
        dp = Lr["depth"]
        sl = max(1e-4, float(tr.get("scale", 1.0)))
        S = sa * sl
        # layer empty, in Art-local units: world = sa*(T' + sl*p) + t_a  ==  (sa*p + t_a)*sl + T_l
        ta = Vector((art_ob.location.x, art_ob.location.y))
        Tl = Vector((float(tr.get("x", 0.0)), float(tr.get("y", 0.0))))
        Tp = (sl * ta + Tl - ta) / sa
        z_world = float(dp.get("z", 0.0)) * explode + LAYER_EPS + dz
        lay = _empty(f"BIS Layer {lid}", col, art_ob)
        lay.location = (Tp.x, Tp.y, z_world / sa)
        lay.scale = (sl, sl, sl)
        names = {lay.name}
        thickness = max(0.0, float(dp.get("thickness", 0.1)))
        th_local = thickness / S
        bevel_local_req = float(dp.get("bevel", 0.045)) / S
        bevel_local, route = geometry.effective_bevel(bevel_local_req, th_local, float(g.get("safeRadius", 1.0)))
        if route == "gn":
            stats["gnFallback"] += 1
        if bevel_local < bevel_local_req - 1e-9:
            stats["clampedBevel"] += 1
        segments = int(dp.get("bevelSegments", 6))
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
        if Lr.get("mode") == "combined" or touching_opaque(g, images):
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
        # raster regions are extruded along their alpha-traced contour, which cuts off soft alpha (a neon
        # tube's glow halo, a soft shadow disc): such images also get a flat halo card under the piece. A
        # region that is mostly soft (Vanced Neon's glow layer: 55 % of its pixels) is a glow, not an object:
        # extruded, its traced band became a second glass ring around the tube (QA round 3 #4) - card only.
        soft = {eid: soft_alpha_fraction(im["path"]) for eid, im in images.items()
                if eid in region_ids and not im.get("opaque")}
        cards += [images[eid] for eid, f in soft.items() if f > HALO_SOFT_MIN]
        glow_ids = {eid for eid, f in soft.items() if f > GLOW_SOFT_MIN}
        if glow_ids and Lr.get("mode") != "combined":
            pieces = [p for p, r in zip(pieces, g["regions"]) if r["elementId"] not in glow_ids]
            stats["glowCards"] = stats.get("glowCards", 0) + len(glow_ids)

        # ---- material ----------------------------------------------------------------------------------
        bbox = tuple(g.get("bbox") or (-1, -1, 1, 1))
        paint = paint_spec(Lr.get("fill") or {"type": "auto"}, g.get("texturePath"), bbox)
        alpha = any(p[3] < 0.999 for p in pieces) or bool(paint.get("has_alpha"))
        blend = str(Lr.get("blendMode", "normal"))
        boost = float(env.get("emission", 0.0)) + (0.6 if blend in ("plus-lighter", "screen", "lighten") else 0.0)
        mpar = dict(Lr["material"].get("params") or {})
        spec = materials.make_spec(
            preset, mpar if preset == Lr["material"]["preset"] else {}, paint,
            mono=env.get("mono"), clear=bool(env.get("clear")), alpha=alpha,
            edge_dark=float(env.get("edgeDark", 0.0) or 0.0),
            shadow=dict(Lr.get("shadow") or {}), role=role, thickness=th_local, light=L, bbox=bbox,
            inflate=float(dp.get("inflate", 0.0)), emission=boost, lit=getattr(self, "_lit", 1.0),
            preview_color=pieces[0][4] if pieces else (0.8, 0.8, 0.8), cm=getattr(self, "_cm", "neutral"),
        )
        if preset == "tinted_glass" and spec["shadow"].get("kind") == "neutral":
            spec["shadow"]["kind"] = "chromatic"
        mat_name = f"BIS Mat {lid}"
        mat = materials.ensure(mat_name, spec)
        self._specs.append(spec)
        mats = {mat_name}

        key_base = {"h": geo_key(g, lid), "lid": lid}
        n_open = sum(geometry.count_open(p[1] or []) for p in pieces)
        if n_open:
            # fills are always closed (an SVG fill closes an open subpath); a flag left open used to sweep the
            # round bevel along the outline as a hollow tube
            log(f"layer {lid}: {n_open} open spline(s) treated as closed fills")
            stats["openSplines"] = stats.get("openSplines", 0) + n_open
        for pid, splines, zoff, op, rgb, raster in pieces:
            if not splines:
                continue
            pmat = mat
            if raster is not None and (Lr.get("fill") or {}).get("type", "auto") == "auto":
                # raster element extruded along its alpha contour: project its own PNG with its exact
                # placement (its alpha is honoured: traced contours may include soft, partly transparent pixels)
                rpaint = {"kind": "texture", "image": raster["path"], "uv": image_uv(raster, bbox),
                          "has_alpha": True}
                rspec = dict(spec, paint=rpaint, params=dict(spec["params"]), alpha=True)
                rname = f"BIS Mat {lid} {pid}"
                pmat = materials.ensure(rname, rspec)
                self._specs.append(rspec)
                mats.add(rname)
            # GN route from the start: the Bevel modifier gets the requested width (use_clamp_overlap does the
            # clamping); a route switched by cusps / failed caps keeps the clamped bevel. Raster pieces (alpha-
            # traced, slightly wobbly contours) always take the GN route: a round curve bevel lenses every
            # wobble of the trace into crinkled highlights, the angle-limited Bevel modifier keeps them clean.
            want = "gn" if raster is not None else route
            kp = {**key_base, "p": pid, "n": len(splines)}
            gbev = bevel_local_req if route == "gn" else bevel_local
            data, proute = geometry.solid_mesh(kp, splines, th_local, bevel_local, want, segments, gn_bevel=gbev)
            if proute != route:
                stats["gnFallback"] += 1
            if self.editable:
                cu, proute = geometry.curve_data(kp, splines, th_local, bevel_local, want, segments, gbev)
                ob = _curve_object(f"BIS {lid} {pid}", cu, col, lay)
            else:
                ob = _mesh_object(f"BIS {lid} {pid}", data, col, lay)
            ob.location = (0.0, 0.0, (thickness / 2.0 + zoff) / S)
            ob.scale = (1.0, 1.0, 1.0)
            ob.color = (*rgb, max(0.0, min(1.0, op)))
            covered = pid.startswith("r") and (lid, int(pid[1:])) in self._hidden
            if ob.visible_transmission == covered:
                ob.visible_transmission = not covered
                ob.visible_glossy = not covered
            if covered:
                stats["hiddenPieces"] = stats.get("hiddenPieces", 0) + 1
            _set_material(ob, pmat)
            if self.editable:
                geometry.apply_route(ob, proute, th_local, float(cu.get("bis_gb", 0.0) or 0.0), segments, pmat)
            names.add(ob.name)
            stats["pieces"] += 1

        for k, im in enumerate(cards):
            bb = im.get("bbox") or bbox
            quad = image_quad(im, bbox)
            ckp = {"card": im.get("path"), "quad": [list(p) for p in quad]}
            cspl = [geometry.poly_spline(quad)]
            data, _ = geometry.solid_mesh(ckp, cspl, CARD_THICKNESS / S, 0.0, "gn", 1)
            if self.editable:
                ccu, _r = geometry.curve_data(ckp, cspl, CARD_THICKNESS / S, 0.0, "gn", 1)
                ob = _curve_object(f"BIS {lid} img{k}", ccu, col, lay)
            else:
                ob = _mesh_object(f"BIS {lid} img{k}", data, col, lay)
            ob.location = (0.0, 0.0, (CARD_THICKNESS / 2.0 + 0.0005) / S)
            ob.color = (1.0, 1.0, 1.0, max(0.0, min(1.0, float(im.get("opacity", 1.0)) * layer_opacity)))
            cpaint = {"kind": "texture", "image": im["path"], "uv": image_uv(im, bbox), "has_alpha": True}
            cspec = materials.make_spec("flat", {}, cpaint, mono=env.get("mono"), alpha=True,
                                        shadow=dict(Lr.get("shadow") or {}), role="opaque",
                                        thickness=CARD_THICKNESS / S, light=L, bbox=tuple(bb),
                                        cm=getattr(self, "_cm", "neutral"))
            cname = f"BIS Mat {lid} img{k}"
            cmat = materials.ensure(cname, cspec)
            _set_material(ob, cmat)
            if self.editable:
                geometry.apply_route(ob, _r, CARD_THICKNESS / S, 0.0, 1, cmat)
            names.add(ob.name)
            mats.add(cname)
            self._specs.append(cspec)
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
        mat = bpy.data.materials.get("BIS Mat Wallpaper") or bpy.data.materials.new("BIS Mat Wallpaper")
        from .nodes import Graph, TopologyMismatch, auto_layout
        key = "wp1"

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
            em = gr.node("ShaderNodeEmission")
            gr.set(em.inputs["Color"], base)
            gr.set(em.inputs["Strength"], 1.0)
            out = gr.node("ShaderNodeOutputMaterial")
            gr.link(em.outputs[0], out.inputs["Surface"])

        if mat.get("bis_key") == key:
            try:
                build(Graph(mat.node_tree, update=True))
            except TopologyMismatch:
                mat["bis_key"] = ""
        if mat.get("bis_key") != key:
            mat.node_tree.nodes.clear()
            build(Graph(mat.node_tree))
            mat["bis_key"] = key
            auto_layout(mat.node_tree)
        if len(ob.data.materials) == 0:
            ob.data.materials.append(mat)
        else:
            ob.data.materials[0] = mat

    def specs(self) -> list:
        return list(self._specs)

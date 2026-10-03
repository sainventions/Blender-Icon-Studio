"""Scene construction: (project, geometry bundle, appearance) -> Blender scene, incrementally.

Layout (PLAN §3, D1): icon in the XY plane, camera on +Z looking −Z, 1 BU = 1 art unit.

    BIS Icon (collection)
      BIS Plate                       canvas space; back at −thickness, front face at z = 0
      BIS Art          (empty)        canvas.art: uniform scale + (x, y)
        BIS Layer <id> (empty)        layer.transform (scale about the canvas origin, then translate)
                                      + z = depth.z · camera.explode + ε  (expressed in Art-local units)
          BIS <id> r<i> / sil / img<k>  curve objects; object coords == art coords (texture projection)
    BIS Rig (collection)              camera, 4 area lights, wallpaper plane

Everything is updated in place between renders: objects/empties are reused by name, curve datablocks
come from geometry's cache (layer hash + depth params), materials update their node values in place.
"""
from __future__ import annotations

import math
import os
from typing import Optional

import bpy
from mathutils import Matrix, Vector

from . import appearance as appearance_mod
from . import geometry, lighting, materials
from .defaults import norm_bundle, norm_camera, norm_project
from .gpu import configure_scene
from .util import gradient_samples, hex_to_linear, hex_to_srgb, log, srgb_to_linear, stable_hash

LAYER_EPS = 0.002          # layer back face above the plate front face
REGION_DZ = 0.001          # zSub step (zSub is interpreted as a sub-layer index)
CARD_THICKNESS = 0.004


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


def _curve_object(name: str, data: bpy.types.Curve, col: bpy.types.Collection,
                  parent: Optional[bpy.types.Object]) -> bpy.types.Object:
    ob = bpy.data.objects.get(name)
    if ob is not None and ob.type != "CURVE":
        bpy.data.objects.remove(ob)
        ob = None
    if ob is None:
        ob = bpy.data.objects.new(name, data)
    elif ob.data != data:
        ob.data = data
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
              overrides: Optional[dict] = None) -> dict:
        """Build/update the scene. ``overrides`` (animations): {'lightAngle', 'explode', 'layerZ': {id: dz}}."""
        self.warnings = []
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

        icon_col, rig_col = self.collections()
        scene = self.scene
        self._specs = []
        rig = lighting.resolve(lighting_spec, env["envScale"], env["keyScale"], overrides.get("lightAngle"))
        L = lighting.key_vector(rig)
        lighting.update_lights(scene, rig_col, rig)
        lighting.update_world(scene, rig, (*hex_to_linear(backdrop_color), 1.0))
        # stack height (for perspective framing): top of the highest visible layer, exploded
        stack_top = max([float(Lr["depth"].get("z", 0.0)) * float(cam.get("explode", 1.0)) +
                         float(Lr["depth"].get("thickness", 0.1)) for Lr in eff["layers"] if Lr.get("visible", True)]
                        + [0.0])
        self._camera(rig_col, cam, full_bleed, stack_top)

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

        # ---- plate ---------------------------------------------------------------------------------
        plate = canvas["plate"]
        plate_ok = plate.get("visible", True) and shape != "none" and (plate["fill"].get("type") != "none")
        if plate_ok:
            pp = plate["material"]["preset"]
            # a glass plate always refracts in EEVEE: it is the big frosted pane over the wallpaper (clear /
            # tinted appearances). Glass layers above then refract the wallpaper instead of the plate - a
            # draft-only approximation (D7); Cycles is exact.
            role = "refract" if materials.is_glass(pp) else "opaque"
            self._plate(icon_col, plate, shape, canvas.get("cornerRadius", 0.225), role, L, env, full_bleed)
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
        wp_kind = env.get("wallpaper") or (("dark" if env.get("dark") else "light") if backdrop == "wallpaper" else None)
        if wp_kind:
            self._wallpaper(rig_col, wp_kind, plate["thickness"], camera_visible=(backdrop == "wallpaper"))
            keep.add("BIS Wallpaper")
            used_mats.add("BIS Mat Wallpaper")

        # ---- cleanup stale BIS objects / materials ---------------------------------------------------
        for ob in list(icon_col.objects) + [o for o in rig_col.objects if o.name == "BIS Wallpaper"]:
            if ob.name not in keep:
                data = ob.data if ob.type == "MESH" else None
                bpy.data.objects.remove(ob, do_unlink=True)
                if data is not None and data.users == 0:
                    bpy.data.meshes.remove(data)
        for m in list(bpy.data.materials):
            if m.name.startswith("BIS Mat") and m.name not in used_mats and m.users == 0:
                bpy.data.materials.remove(m)
        geometry.purge_unused_curves()
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
    def _camera(self, col: bpy.types.Collection, cam: dict, full_bleed: bool, stack_top: float = 0.0) -> None:
        ob = bpy.data.objects.get("BIS Camera")
        if ob is None:
            ob = bpy.data.objects.new("BIS Camera", bpy.data.cameras.new("BIS Camera"))
        if ob.name not in col.objects:
            col.objects.link(ob)
        cd = ob.data
        zoom = max(0.05, float(cam.get("zoom", 1.0)))
        cd.clip_start = 0.05
        cd.clip_end = 200.0
        if cam.get("view", "front") == "front" or full_bleed:
            cd.type = "ORTHO"
            cd.ortho_scale = (2.0 if full_bleed else 2.24) / zoom
            ob.matrix_world = Matrix.Translation((0.0, 0.0, 10.0))
        else:
            cd.type = "PERSP"
            cd.sensor_fit = "AUTO"
            fov = math.radians(max(5.0, min(120.0, float(cam.get("fov", 30.0)))))
            cd.angle = fov
            # frame the ±1.12 icon (+ room for tilt foreshortening), aimed at the middle of the layer stack
            dist = (1.12 / zoom) / math.tan(fov / 2.0) * 1.32 + 0.5 * stack_top
            tx, ty = math.radians(float(cam.get("tiltX", 0.0))), math.radians(float(cam.get("tiltY", 0.0)))
            pos = Matrix.Rotation(ty, 4, "Y") @ Matrix.Rotation(-tx, 4, "X") @ Vector((0.0, 0.0, dist, 1.0))
            pos = Vector(pos[:3])
            target = Vector((0.0, 0.0, 0.4 * stack_top))
            z = (pos - target).normalized()
            up = Vector((0.0, 1.0, 0.0))
            if abs(z.dot(up)) > 0.999:
                up = Vector((0.0, 0.0, -1.0))
            x = up.cross(z).normalized()
            y = z.cross(x)
            m = Matrix((x, y, z)).transposed().to_4x4()
            m.translation = pos
            ob.matrix_world = m
        self.scene.camera = ob

    # -------------------------------------------------------------------------- plate
    def _plate(self, col, plate: dict, shape: str, corner_radius: float, role: str, L, env: dict,
               full_bleed: bool = False) -> None:
        th = max(0.0, float(plate.get("thickness", 0.16)))
        bevel_req = max(0.0, float(plate.get("bevel", 0.04)))
        bevel, route = geometry.effective_bevel(bevel_req, th, 1.0)
        splines = geometry.plate_outline(shape, corner_radius)
        data, route = geometry.curve_data({"plate": shape, "cr": round(corner_radius, 4)}, splines, th, bevel,
                                          route, 8)
        ob = _curve_object("BIS Plate", data, col, None)
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
            preview_color=paint.get("color", (0.9, 0.9, 0.9)),
        )
        mat = materials.ensure("BIS Mat Plate", spec)
        _set_material(ob, mat)
        geometry.apply_route(ob, route, th, bevel_req, 8, mat)
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
        if Lr.get("mode") == "combined":
            pieces.append(("sil", g["silhouette"], 0.0, layer_opacity, (1.0, 1.0, 1.0), None))
        else:
            for i, r in enumerate(g["regions"]):
                zsub = float(r.get("zSub", 0.0))
                zoff = min(zsub, 40.0) * REGION_DZ if zsub >= 1.0 else zsub
                pieces.append((f"r{i}", r["splines"], zoff, float(r.get("opacity", 1.0)) * layer_opacity,
                               paint_rgb(r.get("paint") or {}), images.get(r["elementId"])))
        region_ids = {r["elementId"] for r in g["regions"]}
        cards = [im for eid, im in images.items() if eid not in region_ids]

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
            shadow=dict(Lr.get("shadow") or {}), role=role, thickness=th_local, light=L, bbox=bbox,
            inflate=float(dp.get("inflate", 0.0)), emission=boost,
            preview_color=pieces[0][4] if pieces else (0.8, 0.8, 0.8),
        )
        if preset == "tinted_glass" and spec["shadow"].get("kind") == "neutral":
            spec["shadow"]["kind"] = "chromatic"
        mat_name = f"BIS Mat {lid}"
        mat = materials.ensure(mat_name, spec)
        self._specs.append(spec)
        mats = {mat_name}

        key_base = {"h": geo_key(g, lid), "lid": lid}
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
            data, proute = geometry.curve_data({**key_base, "p": pid, "n": len(splines)}, splines, th_local,
                                               bevel_local, route, segments)
            if proute != route:
                stats["gnFallback"] += 1
            ob = _curve_object(f"BIS {lid} {pid}", data, col, lay)
            ob.location = (0.0, 0.0, (thickness / 2.0 + zoff) / S)
            ob.scale = (1.0, 1.0, 1.0)
            ob.color = (*rgb, max(0.0, min(1.0, op)))
            _set_material(ob, pmat)
            geometry.apply_route(ob, proute, th_local, bevel_local_req if proute == route else bevel_local,
                                 segments, pmat)
            names.add(ob.name)
            stats["pieces"] += 1

        for k, im in enumerate(cards):
            bb = im.get("bbox") or bbox
            quad = image_quad(im, bbox)
            data, _ = geometry.curve_data({"card": im.get("path"), "quad": [list(p) for p in quad]},
                                          [geometry.poly_spline(quad)], CARD_THICKNESS / S, 0.0, "gn", 1)
            ob = _curve_object(f"BIS {lid} img{k}", data, col, lay)
            ob.location = (0.0, 0.0, (CARD_THICKNESS / 2.0 + 0.0005) / S)
            ob.color = (1.0, 1.0, 1.0, max(0.0, min(1.0, float(im.get("opacity", 1.0)) * layer_opacity)))
            cpaint = {"kind": "texture", "image": im["path"], "uv": image_uv(im, bbox), "has_alpha": True}
            cspec = materials.make_spec("flat", {}, cpaint, mono=env.get("mono"), alpha=True,
                                        shadow=dict(Lr.get("shadow") or {}), role="opaque",
                                        thickness=CARD_THICKNESS / S, light=L, bbox=tuple(bb))
            cname = f"BIS Mat {lid} img{k}"
            cmat = materials.ensure(cname, cspec)
            _set_material(ob, cmat)
            geometry.apply_route(ob, "gn", CARD_THICKNESS / S, 0.0, 1, cmat)
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
        ob.location = (0.0, 0.0, -plate_thickness - 0.9)
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
            t = gr.map_range(y, 2.2, -2.2, 0.0, 1.0)
            base = gr.mix_rgb(t, (*hex_to_linear(w["top"]), 1), (*hex_to_linear(w["bottom"]), 1))
            for b in w["blobs"]:
                d = gr.vmath("DISTANCE", pos, (b["x"] * 1.6, b["y"] * 1.6, 0.0))
                f = gr.map_range(d, 0.0, b["r"] * 1.9, 1.0, 0.0, interp="SMOOTHSTEP")
                base = gr.mix_rgb(gr.math("MULTIPLY", f, 0.85), base, (*hex_to_linear(b["color"]), 1))
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

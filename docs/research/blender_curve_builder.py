"""
Reference consumer of svg_prototype.py output inside Blender (bpy only - Blender 5.0 ships
Python 3.11 and none of picosvg/skia-pathops/shapely, so ALL SVG work happens in the backend
venv and Blender only receives JSON splines).

Builds one 2D filled curve object per colour region (occlusion-cut, so regions of one layer do not
overlap), stacked in Z per layer, with flat emission materials (solid or gradient via the
`paint.shader` recipe). Used for the OptiX validation render in svg-pipeline.md.

Run:
  blender -b --factory-startup --python blender_curve_builder.py -- in.json out.png [res] [samples] [gn|legacy]
"""
import json
import sys

import bpy

LAYER_DZ = 0.02     # Z spacing between layers (icon space: longer side = 1.0)
REGION_DZ = 0.001   # sub-offset between regions inside one layer


def new_curve_object(name, splines, z):
    cu = bpy.data.curves.new(name, "CURVE")
    cu.dimensions = "2D"
    cu.fill_mode = "BOTH"          # 2D fill; nested closed splines become holes automatically
    cu.resolution_u = 12
    for s in splines:
        sp = cu.splines.new("BEZIER")
        pts = s["points"]
        sp.bezier_points.add(len(pts) - 1)
        for bp, p in zip(sp.bezier_points, pts):
            bp.handle_left_type = bp.handle_right_type = "FREE"
            bp.co = (p["co"][0], p["co"][1], 0.0)
            bp.handle_left = (p["hl"][0], p["hl"][1], 0.0)
            bp.handle_right = (p["hr"][0], p["hr"][1], 0.0)
        sp.use_cyclic_u = bool(s["closed"])
    ob = bpy.data.objects.new(name, cu)
    ob.location.z = z
    bpy.context.scene.collection.objects.link(ob)
    return ob


def srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def make_material(name, paint, opacity):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    emit = nt.nodes.new("ShaderNodeEmission")
    transp = nt.nodes.new("ShaderNodeBsdfTransparent")
    mix = nt.nodes.new("ShaderNodeMixShader")
    nt.links.new(transp.outputs[0], mix.inputs[1])
    nt.links.new(emit.outputs[0], mix.inputs[2])
    nt.links.new(mix.outputs[0], out.inputs["Surface"])
    shader = paint.get("shader")
    if paint["type"] == "solid" or not shader:
        rgb = paint.get("rgb_linear") or [srgb_to_linear(c) for c in paint["rgb"]]
        emit.inputs["Color"].default_value = (*rgb, 1.0)
        mix.inputs["Fac"].default_value = opacity
        return mat
    tc = nt.nodes.new("ShaderNodeTexCoord")
    if shader["type"] == "linear":
        dot = nt.nodes.new("ShaderNodeVectorMath")
        dot.operation = "DOT_PRODUCT"
        dot.inputs[1].default_value = (shader["dir"][0], shader["dir"][1], 0.0)
        nt.links.new(tc.outputs["Object"], dot.inputs[0])
        add = nt.nodes.new("ShaderNodeMath")
        add.operation = "ADD"
        add.inputs[1].default_value = shader["offset"]
        nt.links.new(dot.outputs["Value"], add.inputs[0])
        t_socket = add.outputs[0]
    else:
        (a, c), (b, d) = shader["matrix"]
        rows = []
        for k, (u, v) in enumerate(((a, c), (b, d))):
            dp = nt.nodes.new("ShaderNodeVectorMath")
            dp.operation = "DOT_PRODUCT"
            dp.inputs[1].default_value = (u, v, 0.0)
            nt.links.new(tc.outputs["Object"], dp.inputs[0])
            ad = nt.nodes.new("ShaderNodeMath")
            ad.operation = "ADD"
            ad.inputs[1].default_value = shader["offset"][k]
            nt.links.new(dp.outputs["Value"], ad.inputs[0])
            rows.append(ad)
        comb = nt.nodes.new("ShaderNodeCombineXYZ")
        nt.links.new(rows[0].outputs[0], comb.inputs[0])
        nt.links.new(rows[1].outputs[0], comb.inputs[1])
        ln = nt.nodes.new("ShaderNodeVectorMath")
        ln.operation = "LENGTH"
        nt.links.new(comb.outputs[0], ln.inputs[0])
        t_socket = ln.outputs["Value"]
    # spread: pad = clamp; reflect/repeat would need ping-pong/fract math nodes
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    els = ramp.color_ramp.elements
    stops = paint["stops"]
    # SVG interpolates in sRGB; Blender ramps interpolate the (linear) values we give them,
    # so densify each stop interval with sRGB-interpolated samples converted to linear.
    samples = []
    for i, s in enumerate(stops):
        samples.append((s["offset"], s["rgb"], s["opacity"]))
        if i + 1 < len(stops):
            n = stops[i + 1]
            for k in range(1, 4):
                t = k / 4
                samples.append((s["offset"] + (n["offset"] - s["offset"]) * t,
                                [x + (y - x) * t for x, y in zip(s["rgb"], n["rgb"])],
                                s["opacity"] + (n["opacity"] - s["opacity"]) * t))
    samples = samples[:32]  # Blender ColorRamp limit
    els[0].position = samples[0][0]
    els[0].color = (*[srgb_to_linear(c) for c in samples[0][1]], samples[0][2])
    els[1].position = samples[-1][0]
    els[1].color = (*[srgb_to_linear(c) for c in samples[-1][1]], samples[-1][2])
    for pos, rgb, a in samples[1:-1]:
        e = els.new(pos)
        e.color = (*[srgb_to_linear(c) for c in rgb], a)
    nt.links.new(t_socket, ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], emit.inputs["Color"])
    alpha = nt.nodes.new("ShaderNodeMath")
    alpha.operation = "MULTIPLY"
    alpha.inputs[1].default_value = opacity
    nt.links.new(ramp.outputs["Alpha"], alpha.inputs[0])
    nt.links.new(alpha.outputs[0], mix.inputs["Fac"])
    return mat


_FILL_GROUP = None


def gn_fill_group():
    """Geometry Nodes 'Fill Curve' (constrained Delaunay) - robust where the legacy 2D curve
    fill (scanfill) breaks: hole contours touching the outer contour at a vertex, e.g. an
    even-odd pentagram or a shape minus a shape that shares a vertex with it."""
    global _FILL_GROUP
    if _FILL_GROUP:
        return _FILL_GROUP
    ng = bpy.data.node_groups.new("BIS_FillCurve", "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    ng.interface.new_socket("Material", in_out="INPUT", socket_type="NodeSocketMaterial")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gi = ng.nodes.new("NodeGroupInput")
    go = ng.nodes.new("NodeGroupOutput")
    fill = ng.nodes.new("GeometryNodeFillCurve")
    if "Mode" in fill.inputs:          # Blender 4.4+/5.x: menu socket
        fill.inputs["Mode"].default_value = "N-gons"
    elif hasattr(fill, "mode"):
        fill.mode = "NGONS"
    setmat = ng.nodes.new("GeometryNodeSetMaterial")  # Fill Curve output carries no material
    ng.links.new(gi.outputs["Geometry"], fill.inputs["Curve"])
    ng.links.new(fill.outputs["Mesh"], setmat.inputs["Geometry"])
    ng.links.new(gi.outputs["Material"], setmat.inputs["Material"])
    ng.links.new(setmat.outputs["Geometry"], go.inputs[0])
    _FILL_GROUP = ng
    return ng


def build(data, fill="gn"):
    vb = data["view_box"]
    for L in data["layers"]:
        for r in L["regions"]:
            name = f"L{L['index']}_{r['uid']}"
            ob = new_curve_object(name, r["splines"], L["index"] * LAYER_DZ + r["z_sub"] * REGION_DZ)
            mat = make_material(name, r["paint"], r["opacity"])
            ob.data.materials.append(mat)
            if fill == "gn":
                ob.data.fill_mode = "NONE"
                mod = ob.modifiers.new("fill", "NODES")
                ng = gn_fill_group()
                mod.node_group = ng
                mod[ng.interface.items_tree["Material"].identifier] = mat
    return vb


def setup_render(vb, res, samples, out_path):
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    prefs = bpy.context.preferences.addons["cycles"].preferences
    prefs.compute_device_type = "OPTIX"
    prefs.refresh_devices()
    for d in prefs.devices:
        d.use = d.type == "OPTIX"
    sc.cycles.device = "GPU"
    sc.cycles.samples = samples
    sc.cycles.use_denoising = False
    sc.cycles.max_bounces = 0
    sc.cycles.transparent_max_bounces = 16
    sc.render.film_transparent = True
    sc.view_settings.view_transform = "Standard"
    sc.view_settings.look = "None"
    aspect = vb[2] / vb[3]
    sc.render.resolution_x = res if aspect >= 1 else int(round(res * aspect))
    sc.render.resolution_y = res if aspect <= 1 else int(round(res / aspect))
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGBA"
    sc.render.filepath = out_path
    cam_data = bpy.data.cameras.new("cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = 1.0          # longer side of the viewBox == 1.0 icon units
    cam = bpy.data.objects.new("cam", cam_data)
    cam.location = (0, 0, 5)
    sc.collection.objects.link(cam)
    sc.camera = cam
    w = bpy.data.worlds.new("w")
    sc.world = w


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:]
    src, out = argv[0], argv[1]
    res = int(argv[2]) if len(argv) > 2 else 256
    spp = int(argv[3]) if len(argv) > 3 else 16
    fill_mode = argv[4] if len(argv) > 4 else "gn"     # "gn" (recommended) | "legacy"
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob)
    data = json.load(open(src))
    vb = build(data, fill_mode)
    setup_render(vb, res, spp, out)
    bpy.ops.render.render(write_still=True)
    print("DEVICE", bpy.context.scene.cycles.device, "->", out)

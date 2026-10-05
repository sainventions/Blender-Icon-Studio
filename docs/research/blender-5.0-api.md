# Blender 5.0 Python API: verified on this machine

**Status:** hands-on verification, 2026-10-03. Every snippet below was run with
`"C:/Program Files/Blender Foundation/Blender 5.0/blender.exe" -b --factory-startup --python <script>`
on Blender **5.0.0 (hash a37564c4df7a, built 2025-11-18)**, Windows 11, i7-12700K, RTX 3070 Ti (driver 591.86).
Unless a section says otherwise, results come from running the code. Where a point comes from the web, the section says so.

Test scripts and raw logs are in the session scratchpad (temporary):
(`apiv/`, not kept in the repo)
(`t1_gpu.py`, `t2_svg.py`, `t3_curves.py`, `t4_nodes.py`, `t4b_nodes.py`, `t5_settings.py`, `t5c_eevee_refr.py`, `t7_comp.py`, `t8_worker.py` with `t8_client.py`, `t9_userprefs.py`, and others).
The full socket and property dumps are in `out/t4_nodes.txt`, `out/t4b_nodes.txt` and `out/t5_settings.txt` in that folder. The key parts are copied below.

---

## 0. TL;DR (decision-relevant)

| Topic | Verified result |
|---|---|
| Engine ids | `'BLENDER_EEVEE'`, `'CYCLES'`, `'BLENDER_WORKBENCH'`. **`'BLENDER_EEVEE_NEXT'` raises TypeError in 5.0.** |
| OptiX | Works. The Cycles log reports "Path tracing on: NVIDIA GeForce RTX 3070 Ti (OptiX)" and "Denoising on: ... (OptiX)". nvidia-smi showed 94-95 % GPU load during GPU renders and 0-1 % during CPU renders. |
| GPU vs CPU (256 px, trivial glass scene) | 512 spp: **OptiX 0.66 s, CPU 3.83 s (5.8x)**. 32 spp: OptiX 0.22 s warm (0.74 s on the first render), CPU 0.53 s. |
| EEVEE warm render in a persistent process (256 px) | **0.06 s (1 spp), 0.08 s (8 spp), 0.10 s (16 spp), 0.14-0.15 s (32 spp)**. A node-topology change (shader recompile) costs about 0.11-0.27 s. Value, transform and curve-geometry changes cost nothing extra. |
| EEVEE first render | 0.8-1.1 s when the NVIDIA shader cache is warm. **11.99 s with a cold driver cache** when raytracing and refraction shaders are used for the first time. The first BLENDED-material variant took 4.9 s. The worker must do a warm-up render at startup. |
| Cycles in the worker | 64 spp: 0.22-0.24 s warm, 0.33 s on the first render. 512 spp: 0.50 s at 256 px and 0.94-0.98 s at 512 px. |
| Worker process | Launch to socket READY takes **1.1-1.2 s**. A blocking socket loop inside `--python` works, as do repeated `render.render(write_still=True)` and `wm.save_as_mainfile`. No leak was seen over 10+ renders. |
| VRAM | EEVEE adds about 55-90 MB resident. The first Cycles GPU render adds about **0.65 GB resident** (CUDA context plus a 472 MB "local memory reserve" per the Cycles log), with transient peaks around +1.2 GB (single nvidia-smi samples up to about +2 GB). `use_persistent_data=True` adds a **further ~0.63 GB** for about 10 % speed, so keep it off. The desktop baseline was 3.2-3.5 GB during these tests. |
| SVG import | `bpy.ops.import_curve.svg` exists, and the core add-on `io_curve_svg` is enabled under `--factory-startup`. It creates **one 2D curve object per path or shape**, in a collection named after the file, in document order. **It ignores `stroke-width` and `fill-rule`.** All objects sit at z=0. Scale is **90 DPI: 1 px = 0.0254/90 m (0.2822 mm)**. |
| Curve fill | Always **even-odd**: winding direction is ignored, so `nonzero` paths get holes they should not have. **Overlapping (non-nested) subpaths fill incorrectly** (area 3.75; union = 3.5, even-odd = 3.0). Pre-process with path booleans before building curves. |
| EEVEE glass | Needs **all** of these: `scene.eevee.use_raytracing=True`, `ray_tracing_method='SCREEN'`, `mat.surface_render_method='DITHERED'`, `mat.use_raytrace_refraction=True`, **and a value linked into Material Output > Thickness**. Without any one of them the glass shows the world/probe colour only. The unlinked socket default is ignored, and BLENDED gets no raytraced refraction. |
| Dispersion | **Not in 5.0.** Neither Principled nor Glass BSDF has a dispersion input. Web sources say it lands in 5.3, Principled only, Cycles only. |
| Compositor | `scene.node_tree` is gone. Use `scene.compositing_node_group = bpy.data.node_groups.new(name, 'CompositorNodeTree')`. `CompositorNodeComposite` is removed, so use `NodeGroupOutput` with an interface output socket. Node options are now input sockets (Glare `Type` = `'Bloom'`). Verified bloom in a background render. |
| User prefs (GUI) | `compute_device_type = 'OPTIX'`, with the OptiX RTX 3070 Ti enabled and the CPU disabled. `gpu_backend='OPENGL'`, `shader_compilation_method='THREAD'`. The file was read only and its SHA1 is unchanged. |

---

## 1. GPU: forcing Cycles to OptiX, with proof

### Verified snippet

```python
import bpy

def enable_optix(scene, use_cpu=False):
    cprefs = bpy.context.preferences.addons["cycles"].preferences
    cprefs.compute_device_type = "OPTIX"        # enum: 'NONE','CUDA','OPTIX','HIP','ONEAPI' (no METAL on Windows)
    cprefs.refresh_devices()                     # re-enumerates; harmless to call repeatedly
    for d in cprefs.devices:                     # d.type in {'OPTIX','CUDA','CPU'}; same GPU appears as both CUDA & OPTIX
        d.use = (d.type == "OPTIX") or (use_cpu and d.type == "CPU")
    scene.render.engine = "CYCLES"
    scene.cycles.device = "GPU"
    scene.cycles.use_denoising = True
    scene.cycles.denoiser = "OPTIX"              # dynamic enum: ['OPTIX', 'OPENIMAGEDENOISE'] on this box
    assert cprefs.has_active_device()
    return [(d.name, d.type, d.use) for d in cprefs.devices]
# -> [('NVIDIA GeForce RTX 3070 Ti','CUDA',False), ('12th Gen Intel Core i7-12700K','CPU',False),
#     ('NVIDIA GeForce RTX 3070 Ti','OPTIX',True)]
```

**Gotcha:** `bpy.ops.wm.read_factory_settings()` also **resets preferences**. Afterwards `compute_device_type` is `'NONE'`, so you must call `enable_optix()` again after every reset. `bpy.ops.wm.read_homefile(use_empty=True)` keeps the preferences; it was verified to keep `'OPTIX'`.

### Proof the GPU is used

1. **Cycles log.** Run with `--log "cycles" --log-level info`:
   ```
   cycles | Testing for pre-compiled kernel ...\addons_core\cycles\lib/kernel_sm_86.cubin.zst.
   cycles | Using precompiled kernel.
   cycles | Local memory reserved 494,927,872 bytes. (472.00M)
   cycles | Using OPTIX layout.
          | Path tracing on: NVIDIA GeForce RTX 3070 Ti (OptiX) [CUDA_NVIDIA GeForce RTX 3070 Ti_0000:01:00_OptiX]
          | Denoising on: NVIDIA GeForce RTX 3070 Ti (OptiX) ...
   ```
   (In 5.0 the old `--debug-cycles` still exists, but the new `--log <categories>` / `--log-level` / `--log-file` system is the one to use.)
2. **nvidia-smi.** Sampled with `nvidia-smi --query-gpu=timestamp,utilization.gpu,memory.used,power.draw --format=csv -lms 200`:
   - OptiX and CUDA 512 spp renders: **94-95 % utilization, 115-141 W**, with memory rising from 3243 to about 3900-4450 MiB.
   - CPU 512 spp render (3.8 s): **0-1 % utilization**.
   - nvidia-smi utilization is averaged over its sample window, so the readings lag the render by about 0.5-1 s.

### Timings (256x256, glass box + plate + area light, single process)

| Render | Time |
|---|---|
| Cycles OptiX 32 spp, 1st render (device init + kernel load) | 0.743 s |
| Cycles OptiX 32 spp, 2nd | 0.221 s |
| Cycles OptiX 512 spp | 0.656 s |
| Cycles CPU 32 spp | 0.532 s |
| Cycles CPU 512 spp | 3.834 s |
| Cycles CUDA 512 spp (1st / 2nd) | 0.708 / 0.691 s |
| EEVEE 32 spp, 1st (shader compile, raytracing off) | 1.065 s |
| EEVEE 32 spp, 2nd / 3rd | 0.087 / 0.089 s |
| EEVEE after a node *value* change | 0.089 s (no recompile) |
| EEVEE after adding a node link (recompile) | 0.245 s |

- At low sample counts the fixed per-render overhead (sync and BVH build, about 0.15-0.2 s) dominates. The GPU advantage shows at production sample counts.
- On this trivial scene OptiX and CUDA perform about the same. OptiX is still preferred for its hardware RT cores and the OptiX denoiser.
- The OptiX kernels are precompiled (`kernel_sm_86.cubin.zst`). The driver caches the OptiX pipeline in `%LOCALAPPDATA%\NVIDIA\OptixCache`, so the first-ever run on a fresh driver can be slower.
- The harmless warning `WARNING HIPEW initialization failed: Error opening HIP dynamic library` is printed on every start. Ignore it.

### GPU memory notes (8 GB card)

| Configuration | VRAM effect |
|---|---|
| EEVEE (raytracing on) | about +55-90 MB resident. One noisy transient sample reached about +0.9 GB. |
| Cycles OptiX, first render | about +640 MB resident until the process exits. It is not released when you switch back to EEVEE, only partially. |
| Cycles OptiX, during a render | about +1.2 GB typical peak. Single 50-200 ms samples reached +1.9 to +2.5 GB, but nvidia-smi on WDDM is noisy. |
| `render.use_persistent_data=True` | +630 MB more (4060 → 4688 MiB) for 0.22 → 0.19 s. **Not worth it.** |
| OIDN on CPU instead of the OptiX denoiser | Lower peak (about +1.2 GB versus +1.9 GB) for similar time at 256 px. |

On exit, VRAM returns to baseline.

Recommendation: keep one worker process, with Cycles OptiX used only for finals. If VRAM gets tight, a separate short-lived "final render" process frees the CUDA context when it exits.

---

## 2. SVG import in 5.0

### Verified facts

- `addon_utils.check("io_curve_svg")` returns `(True, True)`: the add-on is a core add-on in `scripts/addons_core/` and is **enabled under `--factory-startup`**.
- `bpy.ops.import_curve.svg(filepath=...)` returns `{'FINISHED'}`. Its properties are `filepath, filter_glob, directory, files`. Importing 6 shapes took 0.15 s.
- There is also `bpy.ops.wm.grease_pencil_import_svg(filepath=..., resolution=, scale=, use_scene_unit=)`, which creates one `GREASEPENCIL` object. Not useful for us. (`wm.gpencil_import_svg` no longer exists.)

### What the importer creates

The test was a 64x64 SVG containing a rounded-rect `<rect>`, an even-odd ring path, a `nonzero` path whose inner square has the same winding, a `<circle>`, a stroked line and a `<g transform>` triangle.

- **Collection:** `bpy.data.collections['test_icon.svg']`. `collection.objects` order is **document (paint) order**: `['bg','ring','ring_nonzero','dot','stroke','tri']`.
- **Objects:** one `CURVE` object per element, named after the SVG `id`. Each has `dimensions='2D'`, `fill_mode='BOTH'`, `resolution_u=12`, `extrude=0` and `bevel_depth=0`.
- **Splines:** subpaths become multiple `BEZIER` splines in the same curve, so the ring has 2 splines.
- **Handles:** curves use `FREE` handles and lines use `VECTOR` handles.
- **Transforms:** group transforms are baked into the points. Every object has an identity transform and **every object is at z=0**, coplanar, so overlapping shapes z-fight. You must assign z offsets yourself.
- **Materials:** one material per shape, named `SVGMat.NNN`. It has a **Diffuse BSDF** node (not Principled), and its `diffuse_color` is the fill colour converted to linear (for example #2D7FF9 becomes (0.026, 0.212, 0.947)).
- **Strokes are ignored.** The stroked `<path stroke-width=4 fill=none>` was imported as a **3D curve with no bevel and no material**, so it renders nothing.
- **fill-rule is ignored.** The `nonzero` path rendered with a hole, because fill is always even-odd. See the image `img/blender-5.0-api/svg_import_fill_rule.png` (green square).
- **Units:** **90 DPI**. 1 user unit (px) = 0.0254/90 m = 0.00028222 m.
  - `width/height` are honoured: `width=128 viewBox="0 0 64 64"` gives 0.03613 m.
  - With no width or height, the viewBox units are used: a 24-unit viewBox gives 0.00677 m.
  - `mm` units are honoured: `10mm` gives 0.0100 m.
- **Orientation:** Y is flipped. SVG (x, y) maps to Blender (x·s, (H − y)·s), so the document's top-left becomes (0, H·s) and the icon occupies the +X/+Y quadrant starting at the origin.

**Implication:** for our pipeline it is cleaner to **parse the SVG ourselves** (for example with svgelements, or picosvg/skia-pathops to flatten strokes into outlines and resolve fill-rule and overlaps with path booleans), then build curves directly from Python data (section 3). This gives full control over strokes, fill-rule, gradients, z-order and scale.

---

## 3. Building 2D bezier curves from Python

### Verified snippet

```python
import bpy, math
from mathutils import Vector

def make_curve(name, subpaths, extrude=0.0, bevel_depth=0.0, bevel_res=4, offset=0.0,
               bevel_mode="ROUND", fill="BOTH"):
    """subpaths: list of [(co, handle_left|None, handle_right|None), ...]; None handles -> VECTOR (sharp)."""
    cu = bpy.data.curves.new(name, type="CURVE")
    cu.dimensions = "2D"
    cu.fill_mode = fill            # 2D curves: 'NONE','BACK','FRONT','BOTH'  (3D: 'FULL','BACK','FRONT','HALF')
    cu.resolution_u = 12
    cu.extrude = extrude           # half-thickness: geometry spans z in [-extrude-bevel, +extrude+bevel]
    cu.bevel_depth = bevel_depth
    cu.bevel_resolution = bevel_res
    cu.offset = offset             # use offset = -bevel_depth to keep the silhouette at the original outline
    cu.bevel_mode = bevel_mode     # 'ROUND' | 'OBJECT' | 'PROFILE' (cu.bevel_profile: CurveProfile, presets LINE/SUPPORTS/CORNICE/CROWN/STEPS)
    for pts in subpaths:
        sp = cu.splines.new("BEZIER")          # starts with 1 point
        sp.bezier_points.add(len(pts) - 1)
        sp.use_cyclic_u = True
        for bp, (co, hl, hr) in zip(sp.bezier_points, pts):
            bp.co = co
            if hl is None:
                bp.handle_left_type = bp.handle_right_type = "VECTOR"
            else:  # set type BEFORE assigning handle positions
                bp.handle_left_type = bp.handle_right_type = "FREE"   # FREE | VECTOR | ALIGNED | AUTO
                bp.handle_left, bp.handle_right = hl, hr
    ob = bpy.data.objects.new(name, cu)
    bpy.context.scene.collection.objects.link(ob)
    return ob
```

### Fill and hole tests

The filled area was measured on the evaluated mesh, with `extrude=0`:

| Case | Area measured | Expected even-odd / nonzero | Verdict |
|---|---|---|---|
| Disk r=1 | 3.1335 | 3.1416 | OK (12-segment tessellation) |
| Ring, opposite winding | 2.3501 | 2.356 / 2.356 | Hole OK |
| Ring, **same** winding | 2.3501 | 2.356 / **3.14** | **Even-odd** |
| Square with a same-winding inner square | 3.0 | 3.0 / **4.0** | **Even-odd** |
| Island inside a hole (3 levels) | 2.92 | 2.92 | OK (nesting parity) |
| Two **overlapping**, non-nested squares | **3.75** | union 3.5 / even-odd 3.0 | **Wrong**: overlaps must be resolved first |

Geometry Nodes `GeometryNodeFillCurve` also gave even-odd (3.0) on the same-winding squares. In 5.0 its inputs are `Curve`, `Group ID` and `Mode`, and **Mode is now an input socket (menu)**, not a node property.

### Extrude and bevel results (ring r=1 / 0.5)

| Settings | Verts / faces | z range | Top-cap area |
|---|---|---|---|
| `extrude=0.1` | 384 / 288 | ±0.10 | 2.35 |
| `extrude=0.1, bevel_depth=0.05` | 1344 / 1248 | ±0.15 | silhouette grows to x=±1.05 |
| `extrude=0.1, bevel_depth=0.05, offset=-0.05` | 1344 / 1248 | ±0.15 | x=±1.00, **silhouette preserved** |
| `extrude=0, bevel_depth=0.05` (pillow) | 1248 / 1152 | ±0.05 | |
| `PROFILE`, preset `SUPPORTS` | 1728 / 1632 | ±0.18 | |

### Converting to a mesh (all three work in background)

```python
dg = bpy.context.evaluated_depsgraph_get()
me = bpy.data.meshes.new_from_object(curve_ob.evaluated_get(dg))   # 1) no operator, no context needed (preferred)

with bpy.context.temp_override(active_object=ob, selected_objects=[ob],
                               selected_editable_objects=[ob], object=ob):
    bpy.ops.object.convert(target="MESH")                          # 2) operator w/ override (object keeps its name, new mesh 'X.001')
# 3) bpy.ops.object.convert with real selection (select_set + view_layer.objects.active) also works.
```

### Mesh-modifier alternative (verified)

Take the flat filled curve, convert it with `new_from_object` (the result is all triangles), then add modifiers:

```python
sol = mob.modifiers.new("Solidify", "SOLIDIFY"); sol.thickness = 0.1; sol.offset = 1.0; sol.use_even_offset = True
bev = mob.modifiers.new("Bevel", "BEVEL"); bev.width = 0.03; bev.segments = 4; bev.limit_method = "ANGLE"
bev.use_clamp_overlap = True; bev.harden_normals = False
mob.modifiers.new("WN", "WEIGHTED_NORMAL")
```

The Bevel modifier exposes these properties: `width, width_pct, segments, affect, limit_method, angle_limit, offset_type, profile_type, profile, use_clamp_overlap, loop_slide, harden_normals, miter_outer, miter_inner, vmesh_method, custom_profile`, and others.

**Comparison** (image `img/blender-5.0-api/curve_bevel_vs_mesh_modifiers.png`). On a sharp 5-point star with a hole, both approaches render cleanly.

| Approach | Verts / faces | Notes |
|---|---|---|
| Curve bevel | 196 / 182 | Cheaper, and stays parametric (change `bevel_depth` per frame at no cost) |
| Mesh + Solidify + Bevel | 532 / 546 | Allows per-edge control and weighted normals |

**Recommendation:** use curve bevel by default. Fall back to the mesh path only where curve bevel self-intersects on tiny features.

---

## 4. Shader node sockets in 5.0 (full dumps)

Use **identifiers** in code (`node.inputs["Transmission Weight"]` works by identifier or by name).

### Principled BSDF (`ShaderNodeBsdfPrincipled`)

Node properties:
- `distribution`: `'GGX'` or `'MULTI_GGX'` (default)
- `subsurface_method`: `'BURLEY'`, `'RANDOM_WALK'` (default) or `'RANDOM_WALK_SKIN'`

Inputs, in order (identifier = name):

```
Base Color, Metallic, Roughness(0.5), IOR(1.5), Alpha, Normal, Weight(hidden),
Diffuse Roughness, Subsurface Weight, Subsurface Radius, Subsurface Scale, Subsurface IOR(hidden),
Subsurface Anisotropy, Specular IOR Level(0.5), Specular Tint, Anisotropic, Anisotropic Rotation,
Tangent, Transmission Weight, Coat Weight, Coat Roughness(0.03), Coat IOR(1.5), Coat Tint,
Coat Normal, Sheen Weight, Sheen Roughness, Sheen Tint, Emission Color(1,1,1,1),
Emission Strength(0.0), Thin Film Thickness (nm), Thin Film IOR(1.33)
```

Output: `BSDF`. **There is no Dispersion input.**

### Other shader nodes

| Node | Inputs | Outputs / notes |
|---|---|---|
| Glass BSDF (`ShaderNodeBsdfGlass`) | `Color, Roughness, IOR(1.5), Normal, Weight(hidden), Thin Film Thickness, Thin Film IOR` | `BSDF`. `distribution` is `BECKMANN`, `GGX` or `MULTI_GGX`. **No dispersion.** |
| Refraction BSDF | `Color, Roughness, IOR(1.45), Normal` | `distribution` is `BECKMANN` or `GGX` |
| Transparent BSDF | `Color` | `BSDF` |
| Translucent BSDF | `Color, Normal` | |
| Emission | `Color, Strength(1.0)` | `Emission` |
| Mix Shader | identifiers `Fac` (name "Factor"), `Shader`, `Shader_001` | `Shader` |
| Add Shader | `Shader`, `Shader_001` | |
| Volume Absorption | `Color, Density` | `Volume` |
| Principled Volume | `Color, Color Attribute, Density, Density Attribute, Anisotropy, Absorption Color, Emission Strength, Emission Color, Blackbody Intensity, Blackbody Tint, Temperature, Temperature Attribute` | |
| Volume Scatter | | `phase` is `HENYEY_GREENSTEIN`, `FOURNIER_FORAND`, `DRAINE`, `RAYLEIGH` or `MIE` |
| **New:** Volume Coefficients (`ShaderNodeVolumeCoefficients`) | `Absorption Coefficients, Scatter Coefficients, Anisotropy, Emission Coefficients` | |
| Light Path (outputs only) | | `Is Camera Ray, Is Shadow Ray, Is Diffuse Ray, Is Glossy Ray, Is Singular Ray, Is Reflection Ray, Is Transmission Ray, Is Volume Scatter Ray, Ray Length, Ray Depth, Diffuse Depth, Glossy Depth, Transparent Depth, Transmission Depth, Portal Depth` |
| Fresnel | `IOR, Normal` | `Fac` |
| Layer Weight | `Blend, Normal` | `Fresnel, Facing` |
| Gradient Texture | `Vector` | `Color, Fac`. `gradient_type` is `LINEAR`, `QUADRATIC`, `EASING`, `DIAGONAL`, `SPHERICAL`, `QUADRATIC_SPHERE` or `RADIAL` |
| Noise Texture | `Vector, W, Scale, Detail, Roughness, Lacunarity, Offset, Gain, Distortion` | `Fac, Color`. `noise_type` is `FBM` and others; `normalize` is a property. |
| Texture Coordinate | | `Generated, Normal, UV, Object, Camera, Window, Reflection` |
| Mapping | `Vector, Location, Rotation, Scale` | |
| Color Ramp (`ShaderNodeValToRGB`) | `Fac` | `Color, Alpha`. Use `node.color_ramp.elements`. |
| Mix (`ShaderNodeMix`) | type-suffixed identifiers: `Factor_Float, A_Color, B_Color, ...` | `Result_Color` and others. Set `data_type='RGBA'` first. |
| Bump | `Strength, Distance, Filter Width, Height, Normal` | |
| Bevel | `Radius, Normal` | `samples` property |
| Ambient Occlusion | | `Color, AO` |
| Geometry (**`ShaderNodeNewGeometry`**) | | `Position, Normal, Tangent, True Normal, Incoming, Parametric, Backfacing, Pointiness, Random Per Island` |
| Object Info | | `Location, Color, Alpha, Object Index, Material Index, Random` |
| Material Output | `Surface, Volume, Displacement, **Thickness**` | `target` is `ALL`, `EEVEE` or `CYCLES` |
| Ray Portal BSDF | `Color, Position, Direction` | |
| Holdout | | `Holdout` |
| Shader to RGB (EEVEE only) | | `Color, Alpha` |
| Camera Data | | `View Vector, View Z Depth, View Distance` |
| **New:** `ShaderNodeRadialTiling` | | `Segment Coordinates, Segment ID, Segment Width, Segment Rotation` |

Other nodes:
- `ShaderNodeTexGabor` exists.
- There is no Raycast shader node; `ShaderNodeRaycast` is undefined.
- Instantiating `ShaderNodeGeometry` fails; use `ShaderNodeNewGeometry`.

All 98 `ShaderNode*` types instantiate in a material tree. The list is in `t4b_nodes.txt`.

### Material and world defaults in 5.0

`bpy.data.materials.new()` already contains `Principled BSDF` and `Material Output` nodes. `Material.use_nodes` and `World.use_nodes` emit a **DeprecationWarning** ("expected to be removed in Blender 6.0"). Do not touch them. A new world also has a `Background` node.

---

## 5. EEVEE settings (`scene.eevee`, engine `'BLENDER_EEVEE'`)

### Scene properties (defaults under factory startup)

| Property | Default and options |
|---|---|
| `taa_render_samples` | 64 (`taa_samples` = 16 for the viewport) |
| `use_raytracing` | **False** |
| `ray_tracing_method` | `'SCREEN'`; options `'PROBE'`, `'SCREEN'` |
| `ray_tracing_options` (`RaytraceEEVEE`) | `resolution_scale='2'` (1/2/4/8/16), `use_denoise`, `denoise_spatial`, `denoise_temporal`, `denoise_bilateral`, `screen_trace_thickness=0.2`, `trace_max_roughness=0.5`, `screen_trace_quality=0.25` |
| Fast GI | `use_fast_gi=True`, `fast_gi_method='GLOBAL_ILLUMINATION'` (or `AMBIENT_OCCLUSION_ONLY`), `fast_gi_resolution`, `fast_gi_step_count`, `fast_gi_ray_count`, `fast_gi_distance`, `fast_gi_thickness_near/far`, `fast_gi_bias`, `fast_gi_quality` |
| Shadows | `use_shadows=True`, `shadow_ray_count=1`, `shadow_step_count=6`, `shadow_resolution_scale=1.0`, `shadow_pool_size='512'`. Light-level: `light.use_shadow`, `shadow_soft_size`, `shadow_filter_radius`, `shadow_maximum_resolution`, `use_shadow_jitter`, `shadow_jitter_overblur` |
| GI and clamps | `gi_diffuse_bounces=3`, `clamp_surface_indirect=10`, `light_threshold=0.01` |
| Overscan | `use_overscan`, `overscan_size` |
| Volumetrics | `volumetric_*` |

### Material properties for EEVEE

| Property | Default and options |
|---|---|
| `surface_render_method` | `'DITHERED'` (default) or `'BLENDED'` |
| `use_raytrace_refraction` | False. UI label "Raytrace Transmission"; `use_screen_refraction` is a legacy alias. |
| `thickness_mode` | `'SPHERE'` (default) or `'SLAB'` |
| `use_thickness_from_shadow` | False |
| `use_transparency_overlap`, `use_backface_culling`, `use_backface_culling_shadow`, `use_transparent_shadow`, `refraction_depth` | (other relevant flags) |
| `blend_method` | Still exists (`OPAQUE`, `CLIP`, `HASHED`, `BLEND`) but is legacy. Use `surface_render_method`. |

### Verified EEVEE glass recipe

See `img/blender-5.0-api/eevee_refraction_matrix.png` and `eevee_thickness_socket.png`.

```python
sc.render.engine = "BLENDER_EEVEE"
sc.eevee.use_raytracing = True
sc.eevee.ray_tracing_method = "SCREEN"          # 'PROBE' => glass shows only the world/probe
mat.surface_render_method = "DITHERED"          # 'BLENDED' => no raytraced refraction (probe only)
mat.use_raytrace_refraction = True
mat.thickness_mode = "SLAB"                     # flat icon layers are slabs
nt = mat.node_tree
th = nt.nodes.new("ShaderNodeValue"); th.outputs[0].default_value = layer_thickness_m  # e.g. 2*(extrude+bevel)
nt.links.new(th.outputs[0], nt.nodes["Material Output"].inputs["Thickness"])
# NOTE: setting Material Output.inputs["Thickness"].default_value WITHOUT a link had no effect;
# auto thickness (unlinked) on a curve-built ring gave probe-only (black/gray) glass.
```

Results of the test matrix (checker floor, black world, so probe-only glass shows black):
- **Refraction works:** RT + SCREEN + DITHERED + raytrace refraction, in both SLAB and SPHERE modes, with Principled or Glass BSDF, and with `film_transparent=True`.
- **Probe only (black):** scene RT off, PROBE method, BLENDED, or material raytrace refraction off.

**Shader-compile cost:**
- New pipeline variants are expensive on a cold NVIDIA GL shader cache: the first RT+refraction render took **11.99 s**, and the first BLENDED variant took 4.85 s.
- On a warm cache the same steps cost under 1 s and about 0.25 s.
- The cache is invalidated by driver or Blender updates.
- **Mitigation:** the worker should render a 32 px warm-up frame at startup that uses every material and render-method combination we generate.
- Preferences `system.shader_compilation_method` (`'THREAD'` or `'SUBPROCESS'`) and `system.gpu_shader_workers` (0 = auto) exist but were not benchmarked.

---

## 6. Cycles settings and color management

### `scene.cycles` (factory defaults)

| Property | Default and options |
|---|---|
| `device` | `'CPU'`; options `'CPU'`, `'GPU'` |
| `samples` | 4096 (`preview_samples` 1024) |
| `use_adaptive_sampling` | True |
| `adaptive_threshold` | 0.01 |
| `adaptive_min_samples` | 0 |
| `time_limit` | 0 |
| `use_denoising` | True |
| `denoiser` | `'OPENIMAGEDENOISE'`; dynamic options `['OPTIX','OPENIMAGEDENOISE']` |
| `denoising_input_passes` | `'RGB_ALBEDO_NORMAL'` |
| `denoising_prefilter` | `'ACCURATE'` |
| `denoising_quality` | `'HIGH'`; options `HIGH`, `BALANCED`, `FAST` |
| `denoising_use_gpu` | False (OIDN on GPU) |
| `max_bounces` | 12 |
| `diffuse_bounces`, `glossy_bounces` | 4 each |
| `transmission_bounces` | 12 |
| `volume_bounces` | 0 |
| `transparent_max_bounces` | 8 |
| `caustics_reflective`, `caustics_refractive` | True each |
| `blur_glossy` | 1.0 |
| `sample_clamp_direct` | 0 |
| `sample_clamp_indirect` | 10 |
| `film_exposure` | 1.0 |
| `film_transparent_glass` | False; with `film_transparent_roughness` 0.1 |
| `pixel_filter_type` | `'BLACKMAN_HARRIS'` |
| `filter_width` | 1.5 |
| `seed`, `use_animated_seed` | |
| `use_light_tree` | True |
| `use_guiding` | False |
| `use_auto_tile` | True; `tile_size` 2048 |
| `texture_limit_render` | |
| Fast GI | `use_fast_gi`, `fast_gi_method` |

Related properties outside `scene.cycles`:
- **Render:** `scene.render.film_transparent` (False), `use_persistent_data` (False), `resolution_x/y/percentage`, `filepath`, `use_compositing`, `compositor_device` (`'CPU'` or `'GPU'`), `compositor_precision`.
- **Object visibility:** `visible_camera`, `visible_diffuse`, `visible_glossy`, `visible_transmission`, `visible_volume_scatter`, `visible_shadow`, `is_holdout`, `is_shadow_catcher`.
- **MNEE caustics:** `object.cycles.is_caustics_caster`, `object.cycles.is_caustics_receiver`, `light.cycles.is_caustics_light`, `world.cycles.is_caustics_light`.
- **Passes:** `view_layer.use_pass_*` includes `z`, `normal`, `position`, `mist`, `object_index`, `material_index`, `emit`, `environment`, `transmission_*`, `cryptomatte_*` and others.

### Color management (dynamic enums, verified)

| Setting | Options |
|---|---|
| `display_settings.display_device` | `sRGB` (default), `Display P3`, `Rec.1886`, `Rec.2020`, `Rec.2100-PQ`, `Rec.2100-HLG` |
| `view_settings.view_transform` on sRGB | `Standard`, `ACES 1.3`, `ACES 2.0`, **`Khronos PBR Neutral`**, **`AgX` (default)**, `Filmic`, `Filmic Log`, `False Color`, `Raw` |
| `view_transform` on P3 / Rec.1886 / Rec.2020 | `Standard, ACES 1.3, ACES 2.0, AgX, False Color, Raw` |
| `view_transform` on Rec.2100 | adds HDR variants, for example `AgX - HDR 1000 nits` and `ACES 2.0 - HDR 1000 nits` |

`view_settings.look` options depend on the view:
- **AgX:** `None, AgX - Punchy, AgX - Greyscale, AgX - Very High Contrast, AgX - High Contrast, AgX - Medium High Contrast, AgX - Base Contrast, AgX - Medium Low Contrast, AgX - Low Contrast, AgX - Very Low Contrast`
- **Standard, Filmic, Khronos PBR Neutral and Raw:** `None, Very High Contrast, High Contrast, Medium High Contrast, Medium Contrast, Medium Low Contrast, Low Contrast, Very Low Contrast`
- **ACES 1.3 / ACES 2.0:** `None` plus `... - Reference Gamut Compression`

Other color-management properties:
- `view_settings` also has `exposure`, `gamma`, `use_curve_mapping`, `use_white_balance` (with `white_balance_temperature` and `white_balance_tint`) and `is_hdr`.
- `display_settings.emulation` is `'OFF'` or `'AUTO'`.

For icons where colours must match the source SVG, use **`Standard`**. AgX desaturates brand colours. `Khronos PBR Neutral` is a good "faithful but tone-mapped" option.

### Image output

`render.image_settings`:
- `media_type`: `IMAGE`, `MULTI_LAYER_IMAGE` or `VIDEO`. This property is new in 5.0.
- `file_format`: `PNG`, `OPEN_EXR`, `WEBP`, `JPEG`, `TIFF`, ...
- `color_mode`: `BW`, `RGB` or `RGBA`.
- `color_depth`: `8`, `10`, `12`, `16` or `32`.
- `compression`, `exr_codec`, and `color_management` (`FOLLOW_SCENE` or `OVERRIDE`).

`film_transparent=True` with PNG RGBA was verified to give an RGBA 256x256 output.

**Dynamic enum gotcha.** `bl_rna.properties[...].enum_items` returns placeholders for dynamic enums: `['NONE']` for view and display, `[]` for the denoiser, and `['BLENDER_EEVEE']` only for the engine. To list the real options, use this trick:

```python
import re, ast
def dyn_enum(obj, attr):
    try: setattr(obj, attr, "__invalid__")
    except TypeError as e:
        return list(ast.literal_eval(re.search(r"not found in (\(.*\))", str(e)).group(1)))
```

---

## 7. Compositor in 5.0 (brief, verified)

- `scene.node_tree` **no longer exists**. `scene.use_nodes` still exists (True).
- The compositor is now a node-group asset assigned to `scene.compositing_node_group`.
- **`CompositorNodeComposite` is removed** ("Node type undefined"). Output goes through `NodeGroupOutput` and an interface socket.
- Many node options are now **input sockets**. For example, Glare inputs are `Image, Type (menu socket, default 'Streaks'), Quality, Highlights Threshold, Highlights Smoothness, Clamp Highlights, Maximum Highlights, Strength, Saturation, Tint, Size, Streaks, ...`.
- In a background render the compositor **ran** (bloom visible, see `img/blender-5.0-api/compositor_bloom.png`).
- The **Viewer Node image is not created** in background, and `bpy.data.images['Render Result'].pixels` is **empty** (length 0). Get pixels from the written file, or call `bpy.data.images['Render Result'].save_render(filepath=...)` (4 ms at 256 px).

```python
ng = bpy.data.node_groups.new("Comp", "CompositorNodeTree")
ng.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
rl = ng.nodes.new("CompositorNodeRLayers"); gl = ng.nodes.new("CompositorNodeGlare"); out = ng.nodes.new("NodeGroupOutput")
gl.inputs["Type"].default_value = "Bloom"; gl.inputs["Strength"].default_value = 1.0
ng.links.new(rl.outputs["Image"], gl.inputs["Image"]); ng.links.new(gl.outputs["Image"], out.inputs["Image"])
scene.compositing_node_group = ng; scene.render.use_compositing = True
```

---

## 8. Persistent worker (architecture-informing)

**Verified:** a script started with `blender -b --factory-startup --python worker.py -- --port N` can block in a `socket.accept()`/`readline()` loop indefinitely. The same process repeatedly:
- mutates the scene (material values, object transforms, curve data, node topology),
- calls `bpy.ops.render.render(write_still=True)` for both EEVEE and Cycles, switching engines,
- changes `resolution_percentage` (50 % gave 128 px and 200 % gave 512 px),
- writes to arbitrary `render.filepath` values,
- runs `bpy.ops.wm.save_as_mainfile(filepath=..., copy=True)` (4 ms).

The process exits cleanly with code 0 after the loop ends. The full prototype is `t8_worker.py` with its client `t8_client.py`.

### Minimal worker loop

```python
import bpy, socket, json, sys, time, traceback
argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
port = int(argv[argv.index("--port") + 1])
# build scene (bpy.ops.wm.read_homefile(use_empty=True) keeps prefs; read_factory_settings resets them!)
# enable_optix(bpy.context.scene); warm-up render here
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", port)); srv.listen(1)
print("READY", flush=True)                       # parent waits for this line on stdout
conn, _ = srv.accept(); f = conn.makefile("rwb")
while (line := f.readline()):
    msg = json.loads(line)
    try:
        sc = bpy.context.scene
        if msg["cmd"] == "render":
            sc.render.engine = msg.get("engine", "BLENDER_EEVEE")
            sc.render.resolution_percentage = msg.get("pct", 100)
            sc.render.filepath = msg["path"]
            t = time.perf_counter(); bpy.ops.render.render(write_still=True)
            res = {"ok": True, "render_s": time.perf_counter() - t}
        elif msg["cmd"] == "quit":
            res = {"bye": True}
        else:
            res = {"error": "unknown"}
    except Exception:
        res = {"error": traceback.format_exc()}
    f.write((json.dumps(res) + "\n").encode()); f.flush()
    if msg["cmd"] == "quit": break
```

The parent must **drain Blender's stdout** in a thread. Blender prints `Fra:` progress lines and `Saved:` lines for every render, so an undrained pipe can block the worker.

### Measured round trips

All times are 256 px, warm NVIDIA shader cache, measured from the venv client over localhost (overhead under 1 ms).

| Step | Time |
|---|---|
| Process launch → READY (including scene build and OptiX device enumeration) | 1.14-1.20 s |
| EEVEE first render (RT + glass) | **0.82 s** warm cache / **11.99 s** cold driver cache |
| EEVEE 32 spp, repeat | 0.137-0.157 s |
| After changing roughness, z-depth or curve `bevel_depth` | 0.138-0.147 s (no extra cost) |
| After a node-topology change (recompile) | 0.114-0.271 s |
| EEVEE 16 spp, 10 in a row | 0.094-0.115 s each. VRAM flat (3423 → 3423 MiB): **no leak** |
| EEVEE 8 spp / 1 spp | 0.078 / 0.060 s |
| Cycles OptiX 64 spp, 1st / 2nd | 0.33 / 0.22-0.24 s |
| Cycles OptiX 64 spp with `use_persistent_data` | 0.19-0.20 s (+630 MB VRAM) |
| Cycles OptiX 512 spp (256 px / 512 px) | 0.50 / 0.94-0.98 s |
| Switch back to EEVEE after Cycles | 0.15-0.17 s |

Socket overhead is negligible. Bake in a **warm-up render** at worker start, and keep **one worker**.

**Other background-mode notes:**
- `bpy.app.timers` do not fire while the script blocks.
- The `gpu` module's `gpu.platform.*` raises `SystemError: GPU functions for drawing are not available in background mode`, but EEVEE renders fine (offscreen OpenGL context).
- Avoid relying on the `Render Result` pixels; they are empty in background mode.

---

## 9. User's GUI preferences

File: `%APPDATA%/Blender Foundation/Blender/5.0/config/userpref.blend`.

The file was read without `--factory-startup` and with a hard `os._exit(0)` so nothing could be saved. Its SHA1 is `d455e399ed18e18827ae69da2e2b8f249d1f5a0a` and its mtime is 2026-09-13 00:27:41; both were **identical before and after**. `prefs.is_dirty` stayed False.

```
cycles compute_device_type: 'OPTIX'
stored devices: [('NVIDIA GeForce RTX 3070 Ti','OPTIX',use=True,'CUDA_NVIDIA GeForce RTX 3070 Ti_0000:01:00_OptiX'),
                 ('12th Gen Intel Core i7-12700K','CPU',use=False),
                 ('NVIDIA GeForce RTX 3070 Ti','CUDA',use=True)]   # CUDA entry only matters if type=CUDA
system.gpu_backend: 'OPENGL'; shader_compilation_method: 'THREAD'; gpu_shader_workers: 0; memory_cache_limit: 4096
enabled add-ons: io_anim_bvh, io_curve_svg, io_mesh_uv_layout, io_scene_fbx, io_scene_gltf2, cycles, pose_library, bl_pkg
use_preferences_save: True (auto-save on; another reason the worker should use --factory-startup)
```

The GUI is already configured for OptiX. Our worker runs with `--factory-startup` and sets OptiX itself in memory; it never saves preferences.

---

## 10. Surprises and errors (exact)

1. `scene.render.engine = 'BLENDER_EEVEE_NEXT'` fails with:
   `TypeError: bpy_struct: item.attr = val: enum "BLENDER_EEVEE_NEXT" not found in ('BLENDER_EEVEE', 'BLENDER_WORKBENCH', 'CYCLES')`
2. `bpy.types.RenderEngine.__subclasses__()` includes `HydraRenderEngine`, which has no `bl_idname`, so iterating `.bl_idname` raises AttributeError.
3. `gpu.platform.backend_type_get()` in `-b` mode raises `SystemError: GPU functions for drawing are not available in background mode`.
4. `read_factory_settings()` resets Cycles `compute_device_type` to `'NONE'`.
5. `Material.use_nodes` and `World.use_nodes` raise `DeprecationWarning: ... expected to be removed in Blender 6.0`.
6. The SVG importer drops strokes (no bevel, no material), ignores fill-rule (always even-odd), places everything at z=0 and uses Diffuse BSDF materials.
7. Curve fill is always even-odd. Overlapping subpaths give an invalid fill.
8. EEVEE glass is probe-only unless the Material Output **Thickness** socket is linked. This also requires RT on, SCREEN tracing, DITHERED and raytrace refraction on.
9. On a cold NVIDIA shader cache, the first EEVEE RT render takes about 12 s.
10. The `Render Result` image has no pixels in background mode, and no `Viewer Node` image is produced.
11. `CompositorNodeComposite` is undefined, and `scene.node_tree` is gone.
12. `ShaderNodeGeometry` is undefined; the class is `ShaderNodeNewGeometry`.
13. Dispersion is absent in 5.0.
14. Dynamic enums are not listable via `bl_rna`.
15. The 2D curve `fill_mode` values (`NONE/BACK/FRONT/BOTH`) differ from the enum shown by `bl_rna` (`FULL/BACK/FRONT/HALF`).
16. GN Fill Curve `Mode` is now a socket.
17. Every launch prints `WARNING HIPEW initialization failed`. It is harmless.

Sources (web, used only for dispersion):
- [Blender 5.3 Cycles release notes](https://developer.blender.org/docs/release_notes/5.3/cycles/)
- [PR #162041, dispersion in Principled BSDF](https://projects.blender.org/blender/blender/pulls/162041)
- [80.lv article on Principled BSDF dispersion](https://80.lv/articles/principled-bsdf-in-blender-s-cycles-now-supports-dispersion)

---

## Critic verifications

Added by the completeness critic on 2026-10-03. Every item was run headless on Blender 5.0.0 (`-b --factory-startup --python`), RTX 3070 Ti, OptiX. Scripts: `scratchpad/critic/c1_names.py` … `c7_vram.py`. The full cross-doc review is in [`critic-review.md`](critic-review.md).

### Corrections to sections above

| Where | Earlier claim | Verified fact |
|---|---|---|
| §4 Mix row | "type-suffixed identifiers `Factor_Float, A_Color, …`" (implies `inputs["A_Color"]` works) | **`mix.inputs["A_Color"]` and `inputs["Factor_Float"]` raise KeyError.** After `data_type='RGBA'`, `inputs["Factor"]` gives index 0 and `inputs["A"]` gives index 6 (`A_Color`). Use the names, the indices (0 / 6 / 7, output 2), or `next(s for s in n.inputs if s.identifier == "A_Color")`. String lookup is **not** reliably by identifier. |
| §4 Principled list | `Thin Film Thickness (nm)` | The socket name is `Thin Film Thickness` (identifier is the same). `Thin Film IOR` is unchanged. |
| §4 Fresnel row | output `Fac` | Both `outputs["Fac"]` (identifier) and `outputs["Factor"]` (name) resolve. |
| §8 worker notes | "Blender prints `Fra:` progress lines" | In a 5.0 Cycles background render, stdout carried only the log line `render \| Saved: '…'`. There were no `Fra:` or `Sample` lines. Still drain stdout, but **get progress from `bpy.app.handlers.render_stats`** (below). |
| glass doc §7.1 / §7.2 | `use_persistent_data = True` | Keep it **off** (§1: +630 MB VRAM for ~10 %). |

### Compositor

- **Glare** input identifiers and names (`identifier` / name): `Image`, `Type` (MENU), `Quality` (MENU), `Highlights Threshold`/"Threshold", `Highlights Smoothness`/"Smoothness", `Clamp Highlights`/"Clamp", `Maximum Highlights`/"Maximum", `Strength`, `Saturation`, `Tint`, `Size`, `Streaks`, `Streaks Angle`, `Iterations`, `Fade`, `Color Modulation`, `Diagonal Star`/"Diagonal", `Sun Position`, `Jitter`, `Kernel Data Type`, `Float Kernel`/"Kernel", `Color Kernel`/"Kernel".
  - `gl.inputs["Threshold"]` resolves to `Highlights Threshold`, so the glass doc's code is valid.
  - `Type` options: `'Bloom','Ghosts','Streaks','Fog Glow','Simple Star','Sun Beams','Kernel'` (default `'Streaks'`).
  - `Quality` options: `'High','Medium','Low'` (Title Case; default `'Medium'`).
- **Lens Distortion** (`CompositorNodeLensdist`) inputs: `Image, Type, Distortion, Dispersion, Jitter, Fit`. This is the post-process dispersion fallback.

### Properties confirmed to exist

- `scene.cycles.volume_biased` (False), `scene.cycles.film_transparent_glass` (False), `scene.cycles.use_light_tree` (True).
- `view_layer.cycles.use_pass_shadow_catcher`, `material.use_transparent_shadow`.
- Light linking: `ob.light_linking.receiver_collection` and `ob.light_linking.blocker_collection`.
- `ShaderNodeTexSky.sky_type`: `SINGLE_SCATTERING`, `MULTIPLE_SCATTERING` (default), `PREETHAM`, `HOSEK_WILKIE`.
- `Image.alpha_mode`: `STRAIGHT`, `PREMUL`, `CHANNEL_PACKED`, `NONE`. Needed for raster SVG elements as textures.

### Output formats (turntables and exports)

- `image_settings.media_type`: `IMAGE`, `MULTI_LAYER_IMAGE`, `VIDEO`. With `VIDEO` the only `file_format` is `FFMPEG`.
- `render.ffmpeg.format`: `MPEG4, MKV, WEBM, AVI, DV, FLASH, MPEG1, MPEG2, OGG, QUICKTIME`.
- `render.ffmpeg.codec`: `NONE, AV1, H264, H265, WEBM, DNXHD, DV, FFV1, FLASH, HUFFYUV, MPEG1, MPEG2, MPEG4, PNG, PRORES, QTRLE, THEORA`. Other properties include `constant_rate_factor` and `ffmpeg_preset`.
- `IMAGE` file formats: `JPEG, OPEN_EXR, PNG, WEBP, BMP, CINEON, DPX, IRIS, JPEG2000, HDR, TARGA, TARGA_RAW, TIFF`.
- **No GIF, animated WebP or APNG in Blender.** Render a PNG sequence instead. In the venv, Pillow 12.3 was verified to write animated GIF, WebP and APNG, plus **ICO** (multi-size) and **ICNS** (16-1024) on Windows. A system `ffmpeg` 8.1 (winget) is also on PATH.

### Geometry Nodes availability

- **Exist:** `GeometryNodeFillCurve` (inputs `Curve, Group ID, Mode`; `Mode` default `'Triangles'`, also `'N-gons'`), `ExtrudeMesh`, `CurveToMesh`, `RealizeInstances`, `SetShadeSmooth`, `MeshBoolean`, `SubdivisionSurface`, `SetMaterial`, `Transform`, `ResampleCurve`, `FilletCurve`, `StoreNamedAttribute`, `VolumeToMesh`, and the new SDF-grid nodes `MeshToSDFGrid`, `SDFGridOffset`, `SDFGridFillet`, `GridToMesh`.
- **Do not exist:** any curve-offset node (`GeometryNodeOffsetCurve` / `CurveOffset`) or a bevel node (`GeometryNodeBevel` / `MeshBevel`). Rounded edges in a GN route must come from modifiers after the GN modifier.
- **Fill Curve output carries no material.** Without a `GeometryNodeSetMaterial` node the object rendered with the default white material, even though the curve had a material slot.

### Fill and bevel robustness (decides the geometry route)

Test shape: square ±1 with a triangular hole whose apex touches the outer contour at a shared vertex. The expected area is 3.5.

| Build | Result |
|---|---|
| Legacy 2D curve fill, flat (no bevel), two touching contours | **area 1.5: broken** (confirms svg-pipeline.md) |
| Same, with the hole apex nudged 1e-3 away | 3.5005: OK |
| Same shape as **one self-touching ("pinch") contour** | 3.5: OK |
| Legacy curve, `bevel_depth=0.05`, `offset=-0.05` (pill), touching contours | top cap 3.1231 vs 3.1224 for the nudged version: **OK** (the offset separates the contours) |
| GN Fill Curve, touching or pinch | 3.5: OK |
| Curve object + modifiers `NODES` (Fill Curve) → `SOLIDIFY` → `BEVEL` | Accepted, evaluated in 3 ms. **`WEIGHTED_NORMAL` is rejected on curve objects** (mesh objects only). |

Thin features. A strip 0.08 wide (half-width 0.04) with pill bevel and `offset=-bevel`:

| `bevel_depth` | Legacy curve top-cap area | Verdict |
|---|---|---|
| 0.02 | 0.0384 | OK |
| 0.035 | 0.0093 | OK (expected (0.08−0.07)·0.93) |
| 0.05 (> half-width) | 0.018 | **Inverted, self-intersecting cap**: the inset outline crosses over |

GN Fill Curve → Solidify → Bevel (`use_clamp_overlap=True`) on the same strip gave **no flipped faces at any width**. The bevel is clamped automatically.

**Rule:** with the curve route, `bevel_depth` must stay below the layer's thinnest half-width. On the real 68-icon corpus, after culling off-canvas elements, 54 % of icons have a layer whose safe radius is below 0.05 art units (icon width 2.0), and 30 % have one below 0.03. See critic-review.md.

**Shading** (EEVEE close-ups, glossy coat, star with a round hole):
- The curve bevel route was the cleanest.
- GN Fill Curve in `Triangles` mode + Solidify + Bevel + Set Shade Smooth showed dark streak artifacts on the flat cap near the hole, with or without `harden_normals`.
- `N-gons` mode was much cleaner.
- If you use the modifier route, use `Mode='N-gons'` and build it on a **mesh** object (so Weighted Normal is allowed), reading the curve through Object Info or converting it with `new_from_object`.

### Render progress and cancellation

- `bpy.app.handlers.render_stats` is called with **`(stats: str, None)`**, not `(scene, …)`. A 0.4 s render fired about 46 calls. The strings look like `'Mem: 1M | Synchronizing object | Cube'`, `'Mem: 404M | Sample 0/256'`, `'Mem: 461M | Finished'` and `'Time: 00:00.41 (Saving: 00:00.08)'`. Parse `Sample (\d+)/(\d+)` for progress events. Updates are throttled, so short renders show few samples.
- Handlers available: `render_cancel, render_complete, render_init, render_post, render_pre, render_stats, render_write`. `bpy.ops.render.render()` blocks the worker's socket loop. **The only practical way to cancel a running final is to kill the process.** That argues for running finals in a separate, short-lived Blender process; it also frees the ~0.65 GB CUDA context (§1).

### VRAM at final resolutions (Cycles OptiX, 64 spp, trivial glass cube + plane, film transparent)

Desktop baseline 3495 MiB, sampled every 100 ms:

| Denoiser | 1024 px | 2048 px |
|---|---|---|
| **OptiX** | peak **+1.54 GB**, 1.15 s | peak **+2.32 GB**, 3.1 s |
| OIDN (GPU) | peak +1.65 GB, 0.97 s | peak **+2.96 GB**, 3.4 s |

With a desktop baseline that can reach ~4.7 GB, OIDN-GPU at 2048 px would leave under 0.4 GB headroom. **Use the OptiX denoiser for all tiers**, as the user asked ("use optix") and as PLAN.md says. Allow OIDN-GPU only at ≤ 1024 px, and use OIDN-CPU (`denoising_use_gpu=False`) when VRAM is tight.

# Critic review of the four research docs

**Scope.** This review covers `apple-icon-composer.md`, `glass-materials-blender.md`, `blender-5.0-api.md` and `svg-pipeline.md`. Each was checked against the others and against `docs/PLAN.md` (the implementation contract).

**Method.**
- Cheap facts were re-verified hands-on, in Blender 5.0.0 headless with OptiX and in the project venv.
- The SVG prototype was run on the **real 68-icon corpus** (`samle icons/`). None of the four docs had tested on it.
- Verified Blender facts were appended to `blender-5.0-api.md` under **"Critic verifications"**.
- Test scripts live in `scratchpad/critic/` (temporary). Date: 2026-10-03.

The user's standing instruction for this run was **"use optix"**. Every render recommendation below uses the OptiX device. It also uses the OptiX denoiser unless a row says otherwise.

---

## 0. Decision sheet (resolves the contradictions)

| # | Topic | Decision | Why |
|---|---|---|---|
| D1 | Coordinate system | **PLAN.md wins.** Art space spans −1..1 on the longer viewBox side, y up. The icon lies in Blender's **XY plane**, the camera sits on +Z looking −Z, and 1 BU = 1 art unit. | Three docs disagreed (see C1). The glass doc's lights and thicknesses were already tuned for a ±1 icon, so they carry over once the axes are remapped. |
| D2 | SVG JSON scale | Change `svg_prototype.icon_space` to `S = max(vbW, vbH)/2`. This multiplies all splines **and gradient recipes** by 2. | The prototype emits ±0.5 coordinates. |
| D3 | Layer geometry route | **Primary:** legacy 2D curve with `extrude` + `bevel_mode='ROUND'` + `offset=-bevel`. It is the cleanest shading, parametric, and the PLAN default. Required **guards**:<br>(a) cull or clip geometry to the viewBox;<br>(b) clamp `bevel_depth ≤ 0.9 × layer safe radius`;<br>(c) for flat layers (bevel 0), use GN Fill Curve or nudge touching contours apart by 1e-3.<br>**Fallback** per layer: when the clamp cuts the bevel below about 30 % of the request, switch that layer to a mesh object with GN Fill Curve (`Mode='N-gons'`) + Set Material → Solidify → Bevel (`use_clamp_overlap`) → Weighted Normal. | Verified in Blender (see API doc "Critic verifications"):<br>- legacy flat fill breaks on touching contours (area 1.5 vs 3.5), but the beveled+offset version does not;<br>- a bevel larger than the half-width inverts caps;<br>- the GN+modifier route is robust but shows cap shading streaks in `Triangles` mode. |
| D4 | `use_persistent_data` | **Off** in every tier. | Costs +630 MB VRAM for about 10 % speed (API doc §1). The glass doc's §7.1/§7.2 "True" is overruled. |
| D5 | Denoiser | **OptiX denoiser in all Cycles tiers.** OIDN-GPU is an opt-in at ≤1024 px. Use OIDN-CPU when VRAM is tight. | Measured peaks: OptiX +2.32 GB vs OIDN-GPU **+2.96 GB** at 2048 px. Desktop baseline is 3.5–4.7 GB. This also matches the user's "use optix" and PLAN §8. The glass doc's final-tier OIDN-GPU is overruled. |
| D6 | View transform | Default **Khronos PBR Neutral** for lit 3D renders. Offer **Standard** for a "flat / SVG-faithful" mode (emission-only preview, color checks) and AgX Punchy as "photographic". | API doc said Standard and the glass doc said Khronos; the glass doc had the render comparison. |
| D7 | Draft vs faithful preview | EEVEE draft **cannot show glass seen through glass**: only the top glass group refracts (glass doc §4). Treat EEVEE and three.js as "layout/lighting drafts". The **Cycles 512 px / 32–48 spp tier is the first faithful glass view**. Measured at 0.35–0.65 s, so it can run automatically after an edit settles (debounce about 600 ms). | Liquid Glass icons are typically 2–4 stacked glass groups. |
| D8 | Finals and cancel | Live drafts and previews use the persistent worker. **Finals, exports and turntables use a separate short-lived Blender process** (kill = cancel; it frees ~0.65 GB of CUDA context). Progress comes from the `render_stats` handler. | `bpy.ops.render.render` blocks the socket loop, and stdout has no sample-progress lines in 5.0 (verified). |
| D9 | Plate | Detected plate → **canvas background** (parametric squircle). **artScale** = canvas plate size ÷ detected plate bbox. For the corpus: plate bbox 466/500 px, so the art must be scaled ×500/466 for full-bleed Apple export. | Corpus plates are ~100-segment polylines covering 93.2 % of the canvas. |

---

## 1. Contradictions between docs (and against PLAN.md)

**C1. Coordinate frames and scale (critical).**

| Source | Plane | Camera | Icon extent |
|---|---|---|---|
| PLAN §3 | XY | +Z looking −Z | ±1 |
| glass doc §2.3/§6 | **XZ, facing −Y** | (0, −10, 0) | plate ±1 (`ortho_scale` 2.35) |
| svg-pipeline §7.1 | (JSON) | — | **±0.5** (longer side = 1.0) |
| `blender_curve_builder.py` | XY | top camera | ±0.5 (`ortho_scale=1`, `LAYER_DZ=0.02`) |

To port the glass rig to PLAN axes:
- view axis −Y → **+Z**; up Z → **+Y**;
- `light_dir(a,e) = (0,0,1)·cos e + (sin a, cos a, 0)·sin e`;
- the world gradient must use Generated **Y** (not Z), and its light-angle Mapping rotation must be about **Z**. **Re-verify the sign**: the glass doc's "minus sign (verified)" only holds in its XZ frame;
- the camera at (0, 0, 10) with `rotation_euler=(0,0,0)`, `ortho_scale ≈ 2.3–2.4`.

Light distances, sizes and energies (K = 350 W at 5–6 BU) were tuned for a ±1 icon, so keep them.

**C2. Geometry route.**
- PLAN and the API doc say curve extrude + bevel.
- svg-pipeline says "do not use legacy fill, use GN Fill Curve".
- Resolved by test: see D3. Note that the glass doc §2 pill recipe (`bevel ≈ 0.4–0.5 × thickness`) **breaks on about half the real icons** without the clamp (§2.4 below).

**C3. `use_persistent_data`:** glass doc True vs API doc off. Resolved as D4.

**C4. Final denoiser:** glass doc OIDN-GPU vs PLAN OptiX vs the user's "use optix". Resolved as D5.

**C5. View transform:** API doc Standard vs glass doc Khronos PBR Neutral. Resolved as D6.

**C6. EEVEE first-render cost:** glass doc 0.5–1.5 s vs API doc **12 s** on a cold NVIDIA cache. Both are true (different cache states). On first launch after a driver or Blender update, budget about 12 s and show a "compiling shaders" state. The warm-up must cover every `surface_render_method` × raytrace-refraction × preset combination.

**C7. svg-pipeline vs PLAN §4.**

| Topic | PLAN §4 | svg-pipeline | Resolution |
|---|---|---|---|
| Stroking and fallback | picosvg strokes→fills with an svgelements fallback | picosvg stroking is wrong on small viewBoxes; strokes are converted by our own prepass; svgelements rejected | Follow svg-pipeline |
| Filters and images | records `<filter>` drop-shadows and traces `<image>` with OpenCV | drops both, with warnings | Add PLAN's filter/image handling to the prototype |
| Plate detection | bbox ≥ 80 % plus a shape classifier | area ≥ 45 % plus ≥ 90 % containment | Both work on the corpus once off-canvas elements are culled |

**C8. Gradient recipe units.** svg-pipeline's `paint.shader` coefficients are in ±0.5 icon space. If they are used with ±1 art-space object coordinates, every gradient is stretched 2×. Fix it together with D2.

**C9. Layer depth defaults are undefined.** The glass doc uses layer 0.12, plate 0.16, bevel 0.04 and gaps of 0.1–0.3 × thickness; the SVG builder uses `LAYER_DZ` 0.02 at half scale; PLAN has nothing. Proposed defaults (art units, icon = 2.0):
- plate depth 0.16, bevel 0.04;
- layer thickness 0.10;
- requested bevel 0.045 (clamped per layer);
- gap between groups 0.03;
- region sub-offset 0.001.

**C10. API doc errors** (fixed in the API doc's critic section):
- `ShaderNodeMix.inputs["A_Color"]` raises KeyError;
- the socket is named `Thin Film Thickness`, without "(nm)";
- there are no `Fra:` stdout lines in 5.0.

The glass doc's Glare `inputs["Threshold"]` and `Quality='High'` turned out to be **correct**: name lookup works and the menu values are Title Case.

**C11. Layer vocabulary.** Icon Composer has groups (≤4, carrying the glass params) that contain layers (fill, opacity, blend). svg-pipeline emits *layers* (with `max_layers=5` = background + 4) that contain *regions*. PLAN never states the mapping. Proposed mapping:

| svg-pipeline | Icon Composer | Blender | Notes |
|---|---|---|---|
| background layer | canvas fill | parametric plate | |
| each foreground layer | one **group** | one depth plane | glass params live here |
| each region | one **layer** | material slot or sub-object | |
| layer silhouette | "Combined" mode | one glass body for the union | |
| regions as their own bodies | "Individual" mode | each region gets its own pill | |

The `.icon` group order is **front→back**, while the split order is bottom→top, so the order must be reversed on export.

---

## 2. Real-corpus findings (68 icons in `samle icons/`; none of the docs tested these)

1. **Structure.**
   - All 68 icons have `viewBox="0 0 500 500"` and use `<style>` class CSS.
   - 64 use `linearGradient`. There are **no radial gradients, `<use>`, `<text>`, `<mask>` or patterns**.
   - 1 icon uses `clipPath`.
   - **61/68 use a filter drop-shadow**: feOffset 0,0, feGaussianBlur σ = 23, black flood at 0.3–0.4 opacity. This is baked lighting that Apple says to remove. Parse it into `element.shadow`, as PLAN asks, and map it to the group's shadow parameter.
2. **Prototype robustness.**
   - 68/68 processed with no exceptions, in 0.01–0.40 s each.
   - Normalization diff against the filter-stripped original is ≤ 0.1 % on 62 icons, and DJI is 1.76 % (gradient).
   - **5 raster icons fail**: Feit 63 % (it is entirely a clipped PNG, giving **0 layers**), iMessage 28 %, Vanced Neon 26 %, Find Device 21 %, Outlook 15 %. Their `<image>` base64 PNGs are dropped.
3. **Off-canvas junk (bug).**
   - 8 icons (Calendar, Classroom, Docs, Game Launcher, Health, Mail, Play Store, Translate) contain a 10 px `#8A8A8A` stroked path **entirely outside the viewBox** (y ≈ 572) inside a filter group.
   - It becomes a visible gray layer and **blocks background detection**.
   - Culling elements that do not intersect the viewBox fixed all 8 (verified). It belongs in `extract_elements`, and should come with clipping of partially outside geometry to the viewBox.
4. **Background detection after the cull:** 61/68 found. The misses:
   - Earth: tiled pieces with no plate, which is legitimate;
   - Syno Photos: a plate with fill+stroke, so the forced unit should still be allowed as the background;
   - Template: plate only;
   - the 4 remaining raster icons (Feit, Outlook, iMessage, Vanced Neon).
5. **Thin features vs bevel.**
   - Per-layer "safe radius" is measured by morphological opening with a 2 % area tolerance, in art units with icon width 2.0: p10 0.020, **median 0.048**, p90 0.112.
   - **54 % of icons have a layer below 0.05**, 30 % below 0.03, and 7 % below 0.02. Ti84 has 0.004 (display text).
   - The glass doc's default pill (thickness 0.12, so bevel 0.05–0.06) inverts geometry on those layers. A **per-layer clamp is mandatory**. The safe radius is cheap to compute with shapely (binary-search `buffer(-d).buffer(d)`).
6. **Faceting.**
   - 49 of 346 foreground contours, in 17 icons, are dense all-straight polylines (≥ 24 vertices, > 95 % straight), as are the corpus plates.
   - With VECTOR or straight handles, glass rims show facet highlights.
   - Add an optional **"smooth polylines"** step: Bezier fitting with corner detection at about 25°, before spline export.

---

## 3. Unverified claims the implementation depends on

| # | Claim | Risk | Cheapest check |
|---|---|---|---|
| U1 | The `.icon` JSON schema is community reverse-engineered: group order, `features[]` gate, `-specializations`, how inverse refraction is encoded. | Medium. `.icon` export may not open in Icon Composer. | A real `.icon` file from Icon Composer 2.0 needs a Mac. Ship `.icon` **export** labelled "beta", and keep import tolerant. |
| U2 | three.js r186 `MeshPhysicalMaterial` transmission can show glass through glass. | High for the live preview. three.js probably has the same limitation as EEVEE (the transmission pass samples a render target). r186 does have `dispersion`, `iridescence`, `anisotropy`, `sheen`, `attenuationColor` (verified in `node_modules`). | A 10-line R3F test with two stacked transmissive slabs. |
| U3 | EEVEE chromatic shadows via "shadow card". | Low or medium; not prototyped. | One EEVEE render. |
| U4 | Final-tier time on real scenes. Measured only on a trivial scene: OptiX 64 spp took 1.15 s at 1024 px and 3.1 s at 2048 px. The glass doc's "10–60 s" is an estimate. | Medium (UX of exports and turntables). | Benchmark a 4-group glass icon at 1024 px with adaptive sampling. |
| U5 | Mappings from Icon Composer parameters to physics: Refraction strength/depth → IOR and bevel radius; Specular Inside/Outside; IC 2.0's "thin dark outline". | Medium (parity feel). | Tune against Apple screenshots. Proposed: depth → bevel/thickness ratio (0.2–0.5), strength → IOR 1.0–1.7, Inside/Outside → rim mask restricted to inner or outer bevel half, dark outline → `(1 − k·Fresnel·max(−N·L, 0))` multiplier on the coat tint. |
| U6 | Apple's squircle is approximated as a superellipse with n ≈ 5 (glass doc). | Low or medium. Apple's continuous-corner shape is not a superellipse. | Extract it from the Apple Design Resources templates. |
| U7 | The world light-angle rotation sign. | Low, but it **must be re-verified after the D1 axis change**. | One 3-angle sweep render. |

---

## 4. Missing information needed to build the app

1. **Appearance renditions in Blender** (Default / Dark / Mono / Clear L/D / Tinted L/D). No doc gives a recipe. Proposal:
   - Dark: plate becomes the System-Dark gradient, plus overrides and a dimmer key light;
   - Mono: perceptual L* remap per region, with the brightest region white and a gray glass tint;
   - Tinted: Mono × the tint hue and intensity applied to the glass Base Color;
   - Clear: every layer becomes white frosted glass, the plate becomes frosted clear glass, rendered with `film_transparent` + `film_transparent_glass` over the user wallpaper (Cycles only) or composited.
   - Each rendition is a cached render variant keyed by the project hash.
2. **Blend modes and opacity in 3D.** Icon Composer has 10 blend modes; a path tracer has no per-object blend modes. Proposal:
   - opacity → Principled `Alpha` (DITHERED);
   - Plus Lighter → additive emission;
   - Multiply / Plus Darker → transmission tint (absorption);
   - all other modes are honored only in flat and `.icon` export, with a "not physical" badge.
3. **Icon Composer translucency's vertical falloff** ("transparent at bottom, color at top") is missing from the glass doc. Implement it as Texture Coordinate Object.Y → Map Range → Alpha or Transmission Weight (EEVEE: alpha only, because Transmission < 1 is grainy).
4. **Paint → material binding.** How SVG solid and gradient paints feed each preset is not specified (glass Base Color tint vs an opaque inlay under the glass vs emission). Proposal:
   - each preset declares which socket takes `paint` (Base Color for plastic/clay/metal; tint for glass; Emission Color for neon);
   - gradients reuse the svg-pipeline ColorRamp recipe;
   - "Combined" glass bodies carry colored regions as a 0.001-offset inlay under the glass cap.
5. **Raster `<image>` elements** (5/68 icons). OpenCV is **not installed** in the venv; it holds picosvg, skia-pathops, shapely, resvg-py, pillow, numpy, fastapi, uvicorn and pydantic. For v1:
   - decode the base64 PNG and place it with its transform;
   - render it as a textured card (`Image.alpha_mode` STRAIGHT, DITHERED alpha), clipped by any clipPath silhouette;
   - for Feit, the clipPath silhouette becomes the plate shape and the image becomes the plate texture.
   - Contour tracing for extrusion, with `opencv-python-headless`, comes later.
6. **Per-platform canvas mapping and export packaging.**
   - Apple wants a full-bleed 1024 canvas without the plate (the fill comes from the plate color). watchOS needs a 1088 circle.
   - Android adaptive needs 108 dp layers with a 72 dp safe zone: foreground = layers, background = plate, monochrome = Mono.
   - macOS ICNS needs Tahoe margins.
   - **Verified tooling:** Pillow 12.3 writes ICO (multi-size), ICNS, and animated GIF, WebP and APNG on Windows. Blender FFMPEG gives H264, AV1, WEBM (VP9) and PRORES. A system ffmpeg 8.1 exists, but don't depend on it; prefer Pillow plus Blender.
7. **Render queue semantics.** Coalescing drafts is in PLAN. The preview auto-trigger, the cancel path and progress are now covered by D7, D8 and the `render_stats` format (in the API doc).
8. **Mono and tint generation algorithm** and small-size legibility checks: listed as beyond-Apple features, with no algorithm given. Proposal: OKLab L remap stretched to [0.25, 1.0], brightest region forced white, a WCAG-contrast check between adjacent regions, and a warning at 29/40/60 px.
9. **The draft-preview story** is not written down in any doc. Proposal:
   - three.js handles exploded/orbit views and instant geometry feedback;
   - the EEVEE draft supplies lighting and specular;
   - Cycles preview is the glass truth (D7).

---

## 5. Fixes applied by this critic

1. **Appended "Critic verifications" to `blender-5.0-api.md`.** It covers:
   - corrections (Mix lookup, the Thin Film socket name, Fresnel names, stdout progress, `use_persistent_data`);
   - the Glare and Lens Distortion sockets and menu values;
   - existence of `volume_biased`, `use_pass_shadow_catcher`, light linking, the sky types and `alpha_mode`;
   - video and image formats;
   - GN node availability (no offset or bevel nodes; new SDF-grid nodes);
   - the Fill Curve missing-material gotcha;
   - fill and bevel robustness tables and the thin-feature inversion;
   - the curve-object modifier rules (`WEIGHTED_NORMAL` rejected);
   - the shading comparison;
   - the `render_stats` handler signature and strings;
   - VRAM and time at 1024 and 2048 px for OptiX vs OIDN-GPU.
2. **Wrote this review**, with the decision sheet D1–D9.
3. **Real-corpus runs** of `svg_prototype.py`: fidelity, background detection, the off-canvas cull fix (verified), the thin-feature distribution and polyline density. No project code was changed. The cull, the safe-radius clamp and the ×2 scale (D2) are left for workstream A.

## 6. Priority actions for the implementers

1. Workstream A (SVG):
   - cull or clip to the viewBox;
   - art-space ×2 (D2), including gradient recipes;
   - per-layer `safe_radius` in the JSON;
   - parse filter shadows into metadata;
   - raster images as textured cards;
   - an optional polyline smoothing step;
   - allow a fill+stroke plate as the background.
2. Workstream B (Blender):
   - PLAN axes (D1) with the glass rig ported;
   - curve route with the bevel clamp, and the GN N-gons fallback (D3);
   - OptiX device + OptiX denoiser everywhere, persistent data off (D4/D5);
   - a separate final-render process (D8);
   - warm-up over every material variant (C6);
   - Khronos PBR Neutral as the default (D6).
3. Workstream C/D (server and UI):
   - auto Cycles preview after edits settle (D7);
   - `render_stats` progress events;
   - `.icon` export marked beta (U1);
   - the appearance-variant matrix (§4.1);
   - a group-count badge (≤ 4).

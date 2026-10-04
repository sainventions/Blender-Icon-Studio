# Blender Icon Studio — Implementation Plan

> Turn existing flat SVG icons into layered, physically-rendered 3D / glass / Liquid-Glass icons.
> Apple's Icon Composer, rebuilt on Blender 5.0 (EEVEE for live drafts, Cycles + OptiX for truth).

Research that informs every decision below lives in `docs/research/` — read the section relevant to your
workstream: `apple-icon-composer.md`, `glass-materials-blender.md`, `blender-5.0-api.md`, `svg-pipeline.md`
(+ `svg_prototype.py`, `blender_curve_builder.py`), and `critic-review.md` (decision sheet D1–D9 — binding).

## 1. Goals

1. **Import** any SVG icon. Primary test corpus: the user's 68 real icons in `samle icons/` (sic — keep the
   folder name). All are 500×500, 63 share an Illustrator template plate path, 61 have baked drop-shadow
   filters, 15 use strokes, 5 embed raster `<image>` PNGs, 8 contain an off-canvas junk stroke (cull it).
2. **Split** semi-automatically into layers (smart/group/color/element/single + manual merge/split/move).
3. **Stack** layers in depth; extrude + round-bevel into real 3D geometry (pill edges = lensing).
4. **Materials** (presets in `shared/presets.json`): Liquid Glass, clear/frosted/stained glass, prism crystal
   (dispersion), jelly, glossy plastic, satin, candy, gummy, chrome, anodized metal, matte clay, iridescent, neon, flat.
5. **Appearances**: light, dark, clear-light/dark, tinted-light/dark (Icon Composer's 6 renditions).
6. **Render** on the GPU: Draft (EEVEE) → Preview (Cycles OptiX, low spp) → Final (Cycles OptiX).
7. **Export** iOS / macOS / watchOS / Android adaptive / Windows ICO / Web+PWA / marketing hero / `.icon` (beta)
   / `.blend`, plus animations (tilt, turntable, light sweep, explode) as MP4/WebP/GIF.
8. **App**: polished dark "pro tool" UI: instant three.js preview + live Blender renders side-by-side.

## 2. Architecture

```
┌──────────── web/ (React 19 + TS + three.js r186 / R3F 9 + drei + Tailwind 4 + zustand 5) ───────────┐
│ Home (samples, recent, import) · Editor (layers | viewport+render | inspector) · Export · Animate    │
└────────────▲────────────────────────────────────────────▲───────────────────────────────────────────┘
             │ REST /api/* (JSON)                          │ WS /ws (job + system events)
┌────────────┴──────────── server/bis/ (FastAPI, Python 3.13 venv, port 8420) ────────────────────────┐
│ svg/ (A) import→elements→split→geometry+textures · projects · jobs · export · system (C)             │
│ blender/bridge.py (C): ONE persistent worker (drafts/previews) + one-shot processes (finals/exports)  │
└────────────▲────────────────────────────────────────────────────────────────────────────────────────┘
             │ TCP 127.0.0.1 JSON-lines (persistent)  |  stdout JSON-lines (one-shot)
┌────────────┴──── blender_worker/ (B) — runs INSIDE Blender 5.0 (Python 3.11, bpy only, no pip deps) ─┐
│ gpu.py · scene.py · geometry.py · materials.py · lighting.py · appearance.py · render.py · worker.py │
│ oneshot.py · swatches.py                                                                             │
└──────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### Binding decisions (from research; critic-review.md D1–D9 + this plan)
- **D1 Axes**: icon in Blender **XY plane**, camera on **+Z looking −Z**, 1 BU = 1 art unit, plate spans −1..1.
  (The glass doc's rig was XZ/−Y — port it: view axis −Y→+Z, up Z→+Y. Re-verify light-angle sign.)
- **D2 Art space** spans −1..1 on the longer viewBox side (svg_prototype used ±0.5 → multiply by 2, incl. gradients).
- **D3 Geometry**: per region/silhouette a 2D bezier curve with `extrude = max(thickness/2 − bevel, 0)`,
  `bevel_mode='ROUND'`, `bevel_depth = bevel`, `offset = −bevel`, smooth. **Clamp bevel ≤ 0.9 × safeRadius.**
  Fallback per layer (clamped bevel < 30 % of request, or bevel 0 with touching contours): mesh via GN
  Fill Curve (`N-gons`) + Set Material → Solidify → Bevel (`use_clamp_overlap`) — see blender-5.0-api.md.
- **D4** `use_persistent_data = False` everywhere. **D5** OptiX device + **OptiX denoiser** in all Cycles tiers.
- **D6** View transform default **Khronos PBR Neutral** (`render.colorMode`), Standard/AgX optional.
- **D7** EEVEE & three.js are layout/lighting drafts (EEVEE can't show glass-through-glass: only the top-most
  glass layer uses raytraced refraction; lower glass layers use "fake glass" (alpha + coat)). **Cycles preview
  (512 px, 48 spp) is the first faithful view** and auto-runs ~1.2 s after edits settle.
- **D8** Drafts/previews → persistent worker. **Final/ultra renders, exports, animations → one-shot Blender
  process** (kill = cancel, frees VRAM). Progress from the `render_stats` handler.
- **D9** Detected source plate → `canvas.plate` (parametric shape, fill copied, converted to canvas coords);
  `canvas.art.scale = 2 / plateBBoxWidth` and offset so the source plate maps exactly onto −1..1.
- **Paint = rasterised layer art.** For each layer the server rasterises the layer's SVG (resvg-py) to an
  edge-padded RGBA PNG covering the art square −1..1 (`u=(x+1)/2, v=(y+1)/2`, 2048 px). Every material reads
  its paint colour from this texture via object-space XY projection (Blender: TexCoord Object → Mapping →
  Image Texture; three.js: ExtrudeGeometry cap UVs = vertex x/y → same mapping). Exact gradients, opacity and
  raster images for free. Layer `fill` overrides (solid / linear / radial / system-*) replace the texture with
  shader nodes. The presets' `paint` field says where paint goes: `tint` (glass Base Color mixed with white by
  `tint`), `base` (Base Color), `emission` (Emission Color).
- **Squircle** = superellipse `|x|^5 + |y|^5 = 1` (plate −1..1). `rounded` = rounded rect with
  `cornerRadius` × 2 radius. `circle` radius 1. `square`. Same formulas in Python (B) and TS (D2).
- **System fills**: `system-light` = linear top→bottom `#ffffff → #e4e5ea`; `system-dark` = `#3a3a3f → #111114`.
- **Light angle**: degrees, 0 = light from the top, positive = clockwise (toward +x). Default −45 (top-left,
  Icon Composer 1.x). `elevation` = degrees away from the view axis (0 = from the camera, 90 = grazing).
- **Default stack on import**: foreground layers get `liquid_glass`, thickness 0.10, bevel
  `min(0.045, 0.9·safeRadius)`, `z_i = i × 0.13` (bottom layer at z = 0), shadow neutral (opacity mapped from the
  source drop-shadow filter: `min(1, floodOpacity/0.6)`, else 0.5). Plate: `satin`, thickness 0.16, bevel 0.04.
- **Ports**: server 8420, Vite dev 5173 (proxy /api, /ws, /files → 8420).

## 3. Coordinate systems

- **SVG space**: viewBox units, y down.
- **Art space**: `art = ((x − cx)·k, −(y − cy)·k)`, `k = 2 / max(w, h)`, (cx, cy) = viewBox centre. Element bboxes,
  gradient coordinates, splines and textures are in art space.
- **Canvas space**: plate −1..1. `canvas = art · art.scale + (art.x, art.y)`; then per-layer
  `transform` (scale about the canvas origin, then translate). `plate.fill` gradients are in canvas space.
- **Z**: plate back at `−plate.thickness`, plate front face at `z = 0`. Layer back face at
  `z = layer.depth.z · camera.explode + small epsilon`; layer occupies `[z, z + thickness]`.
- **Camera (front)**: orthographic, `ortho_scale = 2.24 / zoom` (room for shadows); `fullBleed` → plate shape forced
  to `square` and `ortho_scale = 2.0` (App Store master). Perspective: lens from `fov`, orbit by tiltX/tiltY.

## 4. Workstreams, ownership and interfaces

Agents work in the main checkout on **disjoint paths**, do NOT commit, do NOT edit other workstreams' files
(if you need a change there, write it in your final report). Shared contract files (`server/bis/models.py`,
`web/src/types.ts`, `shared/presets.json`) may only get *additive, backward-compatible* changes, reported.

### A — SVG pipeline · owns `server/bis/svg/**`, `tests/test_svg*.py`
Port `docs/research/svg_prototype.py` into a package (`prepass.py`, `normalize.py`, `elements.py`, `split.py`,
`geometry.py`, `raster.py`, `textures.py`), applying the critic fixes: viewBox cull/clip, ×2 art space, safe radius,
filter drop-shadows → `Element.shadow`, `<image>` → raster elements (`kind='image'`; decode base64, place with its
transform, keep PNG; silhouette = alpha mask contours via OpenCV so images extrude too), polyline smoothing
(optional, corner-preserving bezier fit), fill+stroke plate allowed as background. Public API in
`server/bis/svg/__init__.py`:

```python
@dataclass
class ImportResult:
    source: SourceInfo; elements: list[Element]; layers: list[Layer]; canvas: Canvas; warnings: list[str]

def import_svg(svg_bytes: bytes, filename: str, project_dir: Path, strategy: SplitStrategy = "smart") -> ImportResult
    # writes project_dir/"source.svg", "normalized.svg", "elements.json" (A's internal per-element store)
def split_layers(project_dir: Path, project: Project, strategy: SplitStrategy) -> list[Layer]   # fresh default layers
def merge_layers(project_dir: Path, project: Project, layer_ids: list[str]) -> list[Layer]      # validated (z-order legal)
def split_layer(project_dir: Path, project: Project, layer_id: str, mode: Literal["elements","islands"]="elements") -> list[Layer]
def build_geometry(project_dir: Path, project: Project, url_prefix: str) -> GeometryBundle
    # cached by hash under project_dir/"cache"; writes layer textures (PNG) and layer SVGs there.
    # URLs = f"{url_prefix}/cache/<file>"; texturePath = absolute path. Also writes
    # project_dir/"cache"/f"geometry-{hash}.json" and returns the bundle (its path is that file).
def geometry_path(project_dir: Path, bundle: GeometryBundle) -> Path
def thumbnail_png(svg_bytes: bytes, size: int = 256) -> bytes          # resvg
def layer_thumbnail_png(project_dir: Path, project: Project, layer_id: str, size: int = 96) -> bytes
```
Acceptance: all 68 corpus icons import without exceptions; plate detected on ≥ 61; recomposition diff of the
layer SVGs vs. normalized source ≤ 1 % for all non-raster icons; geometry bundles validate against
`GeometryBundle`; pytest suite green; full-corpus timing report.

### B — Blender worker · owns `blender_worker/**`, `tests/blender/**`, `web/public/swatches/*.png`
Pure-bpy package run by `blender.exe -b --factory-startup --python blender_worker/worker.py -- --port 0 --root <repo>`.
- `gpu.py`: OptiX only (disable CUDA + CPU entries), `scene.cycles.device='GPU'`, OptiX denoiser. Use
  `read_homefile(use_empty=True)` (NOT read_factory_settings — it resets the device type).
- `scene.py` builds the whole scene from `(project, geometry bundle, appearance)`: plate, layers, camera, lights,
  world, compositor (bloom only when neon present). Caches curve datablocks by `LayerGeometry.hash + depth params`;
  updates materials in place when only values change (avoid EEVEE recompiles); rebuilds node graphs only when the
  preset/topology changes.
- `materials.py`: one builder per preset in `shared/presets.json` (recipes in glass-materials-blender.md §3,
  identifiers verified). Paint via the layer texture (or fill override nodes). Shadow wrap (neutral/chromatic).
  EEVEE settings + fallbacks per the support matrix. Mono/tint colour transforms for appearances.
- `lighting.py`: the light-angle rig + procedural studio world (ported to D1 axes), lighting presets.
- `appearance.py`: resolves the 6 renditions (see §5).
- `render.py`: quality tiers (§6), PNG RGBA output, `render_stats` progress, animations (tilt/turntable/float/
  light-sweep/explode) to PNG frames or FFMPEG H264 MP4.
- `worker.py`: TCP JSON-lines server (protocol §7); prints `BIS_WORKER_READY {json}` after warm-up.
- `oneshot.py`: `-- --job <job.json>` runs a single command and streams `BIS_EVENT {json}` lines, then exits.
- `swatches.py`: renders every material preset on a standard swatch scene (Cycles preview, 192 px) into
  `web/public/swatches/<preset>.png`.
Acceptance: tests that launch real Blender render every preset in draft + preview tiers at ≤ 256 px on OptiX,
render real geometry produced by A for ≥ 5 corpus icons (if A's package is ready; else synthetic bundles),
warm draft render ≤ 0.5 s at 512 px, no VRAM growth across 20 renders.

### C — Server · owns `server/bis/{main,config,projects,jobs,system,export,icon_format,util}.py`,
`server/bis/blender/**`, `tests/test_api*.py`, `tests/test_export*.py`, `scripts/**`, `Blender Icon Studio.cmd`, `README.md`
REST + WS per §8, project storage under `workspace/projects/<id>/` (`project.json` + A's files + `renders/`,
`exports/`), job manager (serial GPU queue, live-draft coalescing per project, auto-preview), `BlenderBridge`
(spawn/monitor/restart the persistent worker; drain its stdout continuously; one-shot processes for D8; cancel =
kill), exports (Pillow resizing from a full-bleed 1024 master: iOS appiconset with dark/tinted variants, macOS
`.iconset` + `.icns` with Tahoe margins, watchOS 1088 circle, Android adaptive fg/bg/monochrome + legacy mipmaps,
Windows multi-size ICO, web favicon/apple-touch/PWA + manifest snippet, marketing hero, `.icon` bundle (beta),
`.blend` packed) zipped; system status via `nvidia-smi --query-gpu=...` polled every 2 s and pushed over WS;
serve `web/dist` at `/` when built. Launcher `Blender Icon Studio.cmd` (+ `scripts/start.ps1`): ensure venv/deps,
build web if `web/dist` is stale/missing, start uvicorn, open Edge `--app=http://127.0.0.1:8420`.
Acceptance: pytest API suite green with a stub bridge AND an opt-in real-Blender integration test
(`BIS_REAL_BLENDER=1`): import a corpus icon → geometry → draft render PNG exists and is non-empty.

### D1 — Web app shell · owns `web/src/**` EXCEPT `web/src/viewport/**`, `web/src/lib/shapes.ts`,
`web/src/lib/appearance.ts`, `web/src/lib/materials3d.ts`
Home (hero, drag-drop import, samples grid from `/api/samples`, recent projects), Editor layout (layers panel w/
drag-reorder via @dnd-kit, merge/split/move elements, visibility/lock, re-split strategy menu; centre stage
hosting `<Viewport/>` + Blender render view with compare slider + appearance strip with 6 rendition thumbnails;
inspector: Layer (material gallery w/ swatches, preset params from presets.json, depth/bevel/z/transform, fill
override incl. gradient editor, shadow, opacity, blend, per-appearance override scope), Document (platform,
shape, plate fill/material, art scale/offset, lighting preset + angle dial + elevation/intensity, camera, color mode,
render settings)), top bar (project name, undo/redo, appearance segmented control, platform, view mode, Render ▾,
Animate, Export, Open in Blender), status bar (worker state, OptiX GPU, VRAM bar, util, queue, last render time),
Export & Animate dialogs with job progress + downloads, toasts, keyboard shortcuts. zustand store with undo/redo
and debounced autosave (PUT). API client + WS client with reconnect.

### D2 — 3D viewport · owns `web/src/viewport/**`, `web/src/lib/shapes.ts`, `web/src/lib/appearance.ts`, `web/src/lib/materials3d.ts`
```ts
// web/src/viewport/index.ts
export interface ViewportProps {
  project: Project; geometry: GeometryBundle | null; presets: Presets
  appearance: AppearanceId
  selectedLayerId: string | null
  onSelectLayer: (id: string | null) => void
  onLayerTransform?: (id: string, t: LayerTransform) => void   // drag to move in front view (optional)
  explode: number          // 0..1 UI explode amount (spreads z gaps, auto-orbits camera slightly)
  view: 'front' | 'orbit'
  showGrid?: boolean        // Apple-style icon grid overlay
  className?: string
}
export function Viewport(p: ViewportProps): JSX.Element
export function LightDial(p: { angle: number; onChange: (deg: number) => void; size?: number }): JSX.Element
// web/src/lib/appearance.ts
export function resolveAppearance(project: Project, appearance: AppearanceId): Project   // effective layers/plate
// web/src/lib/shapes.ts
export function plateOutline(shape: PlateShape, cornerRadius: number, segments?: number): Vec2[]
```
R3F scene mirroring Blender: plate (shape + bevel, fill texture/gradient), layers from `geometry.layers[*]`
(regions → THREE.Shape with holes via `parent`, `ExtrudeGeometry` with bevel matching thickness/bevel/segments,
cap UVs → layer texture), MeshPhysicalMaterial approximations of every preset (transmission, thickness, ior,
roughness, clearcoat, iridescence, dispersion, emissive), environment from drei `<Environment>` +
`<Lightformer>`s rotated by the light angle (no network HDRIs), soft shadows, selection outline, hover, exploded
orbit view, Apple grid overlay, transparent checkerboard backdrop, 60 fps on the 3070 Ti.

## 5. Appearance renditions (worker `appearance.py`, mirrored approximately by `resolveAppearance`)
- **light**: base project.
- **dark**: apply `appearances.dark` overrides (default plate fill `system-dark`); environment × 0.6, key × 0.85.
- **clear-light / clear-dark**: apply `appearances.mono`; every visible layer → `liquid_glass` with tint 0,
  frost 0.3, paint → mono luminance (RGB→BW, stretched to 0.25..1, brightest region → white); plate →
  frosted glass (tint 0) over a wallpaper backdrop (light: soft pastel gradient; dark: deep blue-black gradient).
- **tinted-light**: mono luminance × `tint.color` into the glass base colour, plate light frosted tinted.
- **tinted-dark**: plate `system-dark`; foreground = mono luminance × tint (bright, slightly emissive).
- watchOS ignores appearances (always light). Per-layer `LayerOverride` fields replace base values.

## 6. Render quality tiers (OptiX everywhere; never exceed draft/preview during tests)
| Tier | Engine | Size | Samples | Bounces (max/trans/transp/glossy/diffuse) | Process |
|---|---|---|---|---|---|
| draft | EEVEE, raytracing SCREEN, `trace_max_roughness` 0.8, overscan | 512 | 16 TAA | — | persistent |
| preview | Cycles OptiX, adaptive 0.05, OptiX denoise | 512 | 48 | 16/16/16/6/2 | persistent |
| final | Cycles OptiX, adaptive 0.01, min 64, OptiX denoise | 1024 | 384 | 32/32/32/8/4 | one-shot |
| ultra | Cycles OptiX, adaptive 0.005 | 2048 | 1024 | 32/32/32/8/4 | one-shot |
Glass needs `film_transparent_glass=True` and `film_transparent_roughness ≥ max frost` when the backdrop is
transparent. `volume_bounces` 2 only if jelly is used. Caustics off unless explicitly requested.

## 7. Blender worker protocol
- Persistent: TCP 127.0.0.1, newline-delimited JSON. Request `{"id": str, "cmd": str, "args": {...}}`.
  Responses: `{"id","event":"progress","progress":0..1,"message":str}`* then exactly one of
  `{"id","event":"done","result":{...}}` | `{"id","event":"error","error":str,"traceback":str}`.
  Requests are handled serially. Startup line on stdout: `BIS_WORKER_READY {"port":N,"pid":N,"version":"5.0.0",
  "device":"OPTIX","gpu":"NVIDIA GeForce RTX 3070 Ti","warmupSeconds":x}`.
- One-shot: `blender.exe -b --factory-startup --python blender_worker/oneshot.py -- --root <repo> --job <job.json>`;
  job.json = `{"cmd":..., "args":...}`; stdout lines `BIS_EVENT {same event objects}`; exit code 0/1.
- Commands:
  - `ping` → `{pong: true}`; `system_info` → `{version, device, gpu, devices:[...]}`; `shutdown`.
  - `render` args `{project, geometryPath, appearance, quality, size?, camera?, fullBleed?, out, transparent?}` →
    `{path, width, height, seconds, engine, device, samples}`.
  - `animate` args `{project, geometryPath, appearance, quality, size, kind, frames, fps, format('mp4'|'png'), outDir}`
    → `{frames:[paths], video?: path, seconds}`.
  - `save_blend` args `{project, geometryPath, appearance, out, pack: true}` → `{path}`.
  - `swatches` args `{outDir, size, quality}` → `{files:[paths]}`.

## 8. REST API (server, prefix /api)
| Method | Path | Body / result |
|---|---|---|
| GET | `/system` | `SystemStatus` |
| GET | `/presets` | `Presets` (shared/presets.json + `swatch` URLs for existing `web/public/swatches/*.png` → served at `/swatches/<id>.png`) |
| GET | `/samples` | `SampleIcon[]` from `samle icons/` (+ `docs/research/svgtests/`), `url` = raw SVG |
| GET | `/samples/{name}/thumbnail.png` | PNG (resvg, cached) |
| GET | `/projects` | `ProjectSummary[]` (newest first) |
| POST | `/projects` | multipart `file` (+ `strategy`) **or** JSON `{sample: name, strategy?}` → `Project` |
| GET/PUT/DELETE | `/projects/{id}` | `Project` (PUT = full replace, sets updatedAt) |
| POST | `/projects/{id}/duplicate` | `Project` |
| GET | `/projects/{id}/source.svg` | original SVG |
| POST | `/projects/{id}/split` | `{strategy}` → `Project` |
| POST | `/projects/{id}/layers/merge` | `{layerIds}` → `Project` |
| POST | `/projects/{id}/layers/{layerId}/split` | `{mode}` → `Project` |
| POST | `/projects/{id}/elements/move` | `{elementIds, toLayerId \| null (new layer)}` → `Project` |
| GET | `/projects/{id}/geometry` | `GeometryBundle` |
| GET | `/projects/{id}/layers/{layerId}/thumbnail.png` | PNG |
| POST | `/projects/{id}/render` | `RenderRequest` → `Job` |
| POST | `/projects/{id}/renditions` | `{quality}` → `Job[]` (all 6 appearances, for the appearance strip) |
| POST | `/projects/{id}/animate` | `AnimateRequest` → `Job` |
| POST | `/projects/{id}/export` | `ExportRequest` → `Job` (result.zip URL + files) |
| POST | `/projects/{id}/blend` | `{open: bool}` → `Job` (opens Blender GUI with the .blend when done) |
| GET | `/jobs`, `/jobs/{id}` | `Job[]`, `Job` |
| DELETE | `/jobs/{id}` | cancel |
| POST | `/system/worker/restart` | restart persistent worker |
| WS | `/ws` | `WsEvent` stream |
| GET | `/files/projects/{id}/...` | static files from the project dir (renders, cache, exports) |
Renders: `workspace/projects/<id>/renders/<jobId>.png`, URL `/files/projects/<id>/renders/<jobId>.png`.

## 9. Phases
1. Research ✅ → 2. Contracts ✅ → 3. Parallel build (A, B, C, D1, D2) → 4. Integration on the real corpus
→ 5. Adversarial review + UI QA in the browser → 6. Polish (swatches, hero renders, README screenshots).

## 10. Round 2 — Looks, Style transfer and Icon Pack (batch)
Contract: `StyleSpec`, `StyleRequest`, `BatchRequest`, `BatchSource` in models.py / types.ts; `looks` in presets.json.
| Method | Path | Body / result |
|---|---|---|
| GET | `/projects/{id}/style` | `StyleSpec` extracted from the project |
| POST | `/projects/{id}/style` | `StyleRequest` (`look` \| `style` \| `fromProject`) → `Project` (applied + saved) |
| GET | `/looks` | `Record<string, Look>` (also included in `/presets`) |
| POST | `/batch` | `BatchRequest` → `Job` (kind `batch`); result `{items: BatchItemResult[], contactSheet?: url, zip?: url}` |
Apply rules are documented on `StyleSpec` in models.py (bevel clamped to each layer's safeRadius).
Mono floor: the worker uses 0.3 (supersedes 0.25 in §5); the viewport mirrors the worker.

## 11. Round 6 pivot — PHYSICAL rendering (binding; supersedes all earlier material/explode decisions)
**Goal (user):** make it easy to turn SVGs into the layered setup (the splitter already does this well), then let the
renders take real advantage of Cycles physics. Things should look 3D and physical — *perfect colour accuracy is NOT a
goal*. Sliders need not mirror Icon Composer.

**Materials — ONE Principled BSDF per shape.** Every shape object gets its own material (named after layer/element) whose
graph is exactly: pre-processing nodes → ONE `ShaderNodeBsdfPrincipled` → Material Output (target ALL). Allowed
pre-processing: Texture Coordinate / Mapping / Image Texture (the layer art), Mix Color (white → art = `tint`; specular /
coat / sheen tints), Noise → Bump → Normal (`grain`), Noise → Map Range → Thin Film Thickness (`filmVariation`), Value /
RGB / Math nodes. FORBIDDEN: Mix Shader, Add Shader, Emission / Transparent / Glass / Refraction / Volume shader nodes,
Light Path tricks, overlay estimation, glow cards, self-lit bodies. Params = the Principled schema in
`shared/presets.json` (28 params grouped like Blender's panel: Paint, Base, Subsurface, Specular, Transmission, Coat, Sheen,
Emission, Thin Film); presets are only starting values; per-shape overrides via `Layer.elementMaterials`.
- Shadows: real only. `physical` (default) = Cycles' true shadow; `none` = object.visible_shadow False; legacy
  `neutral`/`chromatic` render as `physical`.
- No native dispersion in 5.0 Principled → "Diamond" = IOR 2.4 + thin film. Jelly = transmission + subsurface (no volume).
- Neon glow = compositor Glare (a render setting, not a material trick).
- Appearances change only Principled inputs (clear = white base, transmission 1, roughness ~0.25 over the wallpaper;
  tinted = base = mono × tint; dark = dark plate fill).
- Colour: no pre-compensation for glass. Keep `brand` as the default colour mode.

**Geometry — robust height-field bodies for ALL shapes.** Each piece (individual) / silhouette (combined) is a watertight body
over its 2D outline defined by inward distance d (per-piece max D): top z(d) = e + hb(d) + inflate·D·√(1−(1−min(d/D,1))²),
bottom mirrored; hb(d) = √(b² − (b − min(d, b))²) (round edge of radius b = bevel), e = max(thickness/2 − b, 0).
Thin parts simply taper (z limited by d) → no inverted bevels, no self-intersections at tips/corners (Gemini star!).
Built in Blender with `mathutils.geometry.delaunay_2d_cdt` (dense boundary + interior Steiner points, graded near edges,
hole-aware), smooth normals, cached. The curve-bevel route is retired (fallback only). Viewport mirrors it with poly2tri.
- Roundness slider = bevel / min(thickness/2, D-ish limit); 1 → full pill / sphere lens.

**View — CAD-style POV instead of "explode".** `camera.iso` 0..1 interpolates an orthographic camera from head-on (0) to
isometric (1: pitch 35.264°, yaw 45°) showing the REAL z distances (no artificial spreading), auto-framed. Same in the live
viewport and Blender renders; animation kind `iso` = head-on → iso → head-on. `camera.explode` stays 1 (legacy).

**Round 7 additions (binding):**
- **Poisson inflation** replaces the distance-based dome (which creased thin parts into fins): per body solve
  ∇²u = −4 on the CDT mesh (Dirichlet u = 0 on the outline; numpy conjugate gradient — no scipy in Blender; typed-array
  CG in the viewport), dome = inflate · D · √(u / u_max) with D = the body's max inscribed radius (disc → sphere-like
  dome, thin parts → round tapering tubes, no medial-axis creases). The round-edge rim hb(d) stays.
- **Camera-relative lighting**: the key/rim/fill rig and the studio world are defined relative to the camera, so iso /
  perspective / animated views light like the head-on view (no washed-out mirror glare). `lighting.angle` is relative to
  the view.
- **Real-height stacking** (`shared/presets.json` "geometry"): body height H = thickness + 2·inflate·maxRadius
  (`LayerGeometry.maxRadius`); layers stack z0 = stackLift, z(i+1) = z(i) + H(i) + stackGap (StyleSpec.zGap overrides the
  gap). Used by import defaults, looks/style, batch and the UI Re-stack. Bevel is clamped to thickness/2 only.
- **Dark rendition** stays physical (Principled inputs only) but must keep glyphs readable over the dark plate.

# Blender Icon Studio

**Turn flat SVG icons into layered, physically rendered 3D, glass and Liquid Glass app icons, rendered by Blender 5.0 on your GPU.**

Blender Icon Studio works like Apple's Icon Composer, but Blender does the rendering. Drop in an SVG and the
app splits it into depth layers. Each layer is extruded with real rounded bevels, so the pill edges bend
light. You then pick materials, lighting and appearances. EEVEE renders a live draft, and Cycles with OptiX
renders the faithful glass. One click exports a ready-to-ship icon set for every platform.

<p align="center">
  <img src="docs/img/hero-photos-liquid-glass.png" width="240" alt="Photos icon as Liquid Glass">
  <img src="docs/img/hero-maps-dark.png" width="240" alt="Maps icon, dark appearance">
  <img src="docs/img/hero-photos-exploded.png" width="240" alt="Exploded layer stack">
</p>

<!-- UI screenshots (docs/img/editor.png, home.png, export.png) are added in the polish phase. -->

---

## Features

- **Import any SVG.** Use drag and drop or the built-in sample library (68 real icons in `samle icons/`, plus
  the pipeline test files). Plain, UTF-16 and gzip-compressed (`.svgz`) files are accepted.
  - Gradients, opacity, strokes, clip paths and embedded raster images are all handled.
  - Baked drop-shadow filters become layer shadows.
  - Off-canvas junk is culled.
- **Smart layer split.** Choose a strategy: smart, group, colour, per element or single.
  - Merge, split and move elements between layers. Z-order validation stops overlapping art from being
    "woven" through other layers.
- **Real 3D geometry.**
  - Every layer is a bezier curve extruded to its thickness and round-bevelled.
  - The bevel is clamped to the thinnest feature, so thin strokes never invert.
  - You control depth per layer and can explode the stack for hero shots.
- **15 material presets** (see `shared/presets.json`): Liquid Glass, clear, frosted and stained glass, prism
  crystal, jelly, glossy plastic, satin, hard candy, gummy, chrome, anodized metal, matte clay, iridescent,
  neon (with bloom) and flat 2D.
- **Lighting rigs.** The light angle and elevation dial follows Icon Composer's −45° default. Presets include
  studio, soft, dramatic, top light, dark field, golden, neon night and flat.
- **Six appearances**, matching Icon Composer's renditions: Default, Dark, Clear Light, Clear Dark, Tinted Light
  and Tinted Dark. Per-layer overrides apply per appearance.

  ![The six renditions of one icon](docs/img/renditions-photos.png)
- **Three render paths**, all on the OptiX device:
  - an instant three.js preview;
  - an EEVEE draft (about 0.2 s);
  - a Cycles + OptiX preview, queued automatically about 1.2 s after your edits settle;
  - Final and Ultra tiers for exports and hero shots.
- **One-click export packages** (zip):

  | Target | What you get |
  |---|---|
  | iOS / iPadOS | `AppIcon.appiconset`: 1024 universal + dark + tinted luminosity variants and `Contents.json` |
  | macOS | `AppIcon.icns`, `AppIcon.iconset` and `AppIcon.appiconset`, with the Tahoe 824/1024 body and transparent margins |
  | watchOS | 1024 appiconset + a 1088 circle |
  | Android | adaptive icon (foreground layers / background plate / themed monochrome, 108 dp, all densities), legacy square + round mipmaps, the XML, and a 512 Play Store icon |
  | Windows | multi-size `app.ico` (16–256) + PNGs |
  | Web / PWA | `favicon.ico` (16/32/48), `favicon.svg`, `apple-touch-icon`, 192/512 + maskable icons, `manifest.webmanifest` and a `<head>` snippet |
  | Marketing | perspective hero render + exploded hero (transparent PNG) |
  | Icon Composer | `.icon` bundle (beta): layer SVGs + `icon.json`, with groups ordered front→back and dark/tinted specializations |
  | Blender | the `.blend` scene with packed textures |

- **Animations:** turntable, tilt, float, light sweep and explode, as MP4 (H.264), WebP, GIF or a PNG
  sequence.
- **Looks:** one-click styles that restyle the whole icon — Liquid Glass, Crystal, Frosted, Prism, Candy,
  Clay, Chrome, Neon, Iridescent and Soft 3D (`looks` in `shared/presets.json`). A look sets every layer's
  material, depth, bevel and shadow, restacks the layers (`z = i × zGap`), and sets the plate material plus,
  when the look defines them, the lighting, camera, colour mode and tint. Bevels are always clamped to
  0.9 × each layer's safe radius, so thin strokes never invert. The icon's own plate colour and shape are
  kept unless the look sets them (Neon switches the plate to System Dark).
- **Copy / paste style:** copy the look of one icon and paste it onto another. Pasting works with a copied
  `StyleSpec` or directly from another project (`fromProject`). The copied style holds the dominant layer
  material, depth and shadow, each layer's material by stack position (bottom → top), the median layer gap,
  the plate material, and the lighting, camera, colour mode and tint. A plate *fill* only travels when it's a
  deliberate System Light / System Dark / None fill, because a solid or gradient plate is the icon's own
  brand colour. Use `?plateFill=true&shape=true` to copy the fill and shape anyway.
- **Icon Pack (batch):** pick many sample icons or existing projects, apply one look (or a copied style), and
  render them all in one queued job. Progress reads "Icon 7/24: Maps". If one icon fails, it's marked on its
  tile and the rest of the pack still renders. Finished icons appear while the pack runs. Cancel stops after the
  current icon and keeps the icons already rendered. Your live drafts still run between icons. The result
  has a **contact sheet**: a dark PNG grid with each icon's name under its tile.
  Add an export request and every icon is also exported, then everything is packed into **one zip** with a
  folder per icon plus the contact sheet. Draft and preview batch renders are capped at 512 px.
- **Open in Blender:** saves the scene and opens it in your Blender GUI with your own preferences. On Windows,
  Blender starts through WMI, so it isn't a child of the server or in the server's job object. It stays open
  after the server stops.
- **Pro-tool UI:** a layers panel, a viewport with a compare slider against the Blender render, an inspector,
  a strip of all six renditions, and live GPU / VRAM / queue status.

---

## Quick start

**Requirements**

- Windows 11
- An NVIDIA RTX GPU (OptiX)
- **Blender 5.0** at `C:\Program Files\Blender Foundation\Blender 5.0\blender.exe`. To use another location,
  set `BIS_BLENDER`.
- **Python 3.13** (the `py` launcher)
- **Node.js 22+**

**Steps**

1. Double-click **`Blender Icon Studio.cmd`**. On the first run it:
   - creates `.venv` and installs `requirements.txt`;
   - runs `npm install` and builds the web UI (`web/dist`);
   - starts the server on <http://127.0.0.1:8420>;
   - opens the app in an Edge app window, or in your default browser if Edge is missing.
2. Pick a sample or drop an SVG on the home screen.
3. Edit layers, materials and lighting. The draft and Cycles preview update live.
4. Click **Export**, choose the targets and download the zip.

Close the launcher console to quit. The Blender processes are tied to the server and exit with it, even if
the server is killed.

**Launcher options** (pass them to the `.cmd` or to `scripts\start.ps1`):

| Option | Effect |
|---|---|
| `-Port <n>` | serve on another port |
| `-NoBrowser` | do not open a window |
| `-Rebuild` | force a web rebuild |
| `-SkipBuild` | never rebuild the web UI |
| `-Fake` | run without Blender, using flat Pillow previews (useful for UI work) |
| `-NoWorker` | start the Blender worker on the first render instead of at startup |

If the server is already running, the launcher just opens a new window.

---

## Architecture

```
┌──────────── web/ (React 19 + TS + three.js / R3F + Tailwind 4 + zustand) ───────────────┐
│ Home · Editor (layers | viewport + Blender render | inspector) · Export · Animate        │
└────────────▲───────────────────────────────────────▲─────────────────────────────────────┘
             │ REST /api/*                            │ WebSocket /ws (job + system events)
┌────────────┴──────────── server/bis/ (FastAPI, Python 3.13, port 8420) ──────────────────┐
│ svg/  import → elements → split → geometry + textures                                   │
│ projects · jobs (serial GPU queue) · rendering · export · icon_format · system           │
│ blender/bridge.py: ONE persistent worker (drafts/previews) + one-shot processes (finals)  │
└────────────▲─────────────────────────────────────────────────────────────────────────────┘
             │ TCP 127.0.0.1 JSON-lines (persistent)  |  stdout BIS_EVENT lines (one-shot)
┌────────────┴──── blender_worker/ — runs INSIDE Blender 5.0 (bpy only) ───────────────────┐
│ scene · geometry · materials · lighting · appearance · render · worker · oneshot         │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

- **Projects** live in `workspace/projects/<id>/`:
  - `project.json`;
  - the original and normalised SVG;
  - `cache/`, which holds geometry bundles, layer textures and layer SVGs;
  - `renders/<jobId>.png`;
  - `exports/<jobId>.zip`;
  - `blend/`.

  Everything under a project is served at `/files/projects/<id>/…`.
- **Jobs:**
  - **One global GPU queue.** Live drafts jump ahead of other work and are *coalesced*: a newer live render
    replaces a queued one. A long export pauses between its master renders to let queued live drafts and
    previews through, so the editor stays live while an export runs.
  - **Cancel:** a queued job is dropped. A running one-shot job has its process killed. A running draft or
    preview in the persistent worker cannot be interrupted: it is marked cancelled at once and its result
    is discarded.
  - **Progress** streams over `/ws`.
- **Process model (decision D8):**
  - Drafts and previews use the warm persistent worker: ~0.26 s for a warm 128 px draft on an RTX 3070 Ti.
  - Final and Ultra renders, exports, animations and `.blend` saves each run in a short-lived Blender
    process. Cancelling kills it, which frees VRAM immediately.
- **Data contract:** `server/bis/models.py` ⇄ `web/src/types.ts` (camelCase JSON) and `shared/presets.json`.
- The full plan is in [`docs/PLAN.md`](docs/PLAN.md), and the research is in [`docs/research/`](docs/research/).

### REST API (prefix `/api`)

| Endpoint | Purpose |
|---|---|
| `GET /system`, `GET /system/logs` | status (Blender, worker, GPU, queue) and the tail of the worker's stdout |
| `POST /system/worker/restart` | restart the worker |
| `GET /presets` | material, lighting, platform, quality presets and `looks`, plus swatch URLs |
| `GET /looks` | the named looks `{id: {label, description, style}}` |
| `GET /samples` | the sample library |
| `GET /samples/{name}/thumbnail.png`, `GET /samples/{name}/source.svg` | a sample's thumbnail or raw SVG |
| `GET\|POST /projects` | list projects, or create one (multipart `file`, or JSON `{sample}`) |
| `GET\|PUT\|DELETE /projects/{id}` | read, replace or delete a project |
| `POST /projects/{id}/duplicate` | duplicate a project |
| `GET /projects/{id}/source.svg` | the project's original SVG |
| `POST /projects/{id}/split` | re-split the layers |
| `POST /projects/{id}/layers/merge` | merge layers |
| `POST /projects/{id}/layers/{layerId}/split` | split one layer |
| `POST /projects/{id}/elements/move` | move elements to another layer |
| `GET /projects/{id}/geometry` | the geometry bundle |
| `GET /projects/{id}/style` | copy style → `StyleSpec` (`?plateFill=true&shape=true` also copies the plate fill / shape) |
| `POST /projects/{id}/style` | paste style: exactly one of `{look}`, `{style: StyleSpec}` or `{fromProject}` → the saved `Project` (400 if not exactly one, 404 for an unknown look or project) |
| `GET /projects/{id}/layers/{layerId}/thumbnail.png` | a layer thumbnail |
| `POST /projects/{id}/render` | queue a render → `Job` |
| `POST /projects/{id}/renditions` | the six appearances → `Job[]` |
| `POST /projects/{id}/animate` | queue an animation → `Job` |
| `POST /projects/{id}/export` | queue an export → `Job`; the result holds `zip`, `files` and `previews` |
| `POST /projects/{id}/blend` | save a `.blend` (`{open: true}` launches Blender) → `Job` |
| `POST /batch` | Icon Pack: `BatchRequest` `{sources: [{sample} \| {projectId}], look? \| style? \| fromProject?, strategy, quality, size, appearance, export?}` → `Job` (kind `batch`); the result holds `items` (`BatchItemResult[]`), `contactSheet` and, with `export`, `zip` (files under `/files/batches/<jobId>/`); while it runs (and after a cancel) `result` is partial: `{items, count, failed, partial: true}` |
| `GET /jobs`, `GET /jobs/{id}`, `DELETE /jobs/{id}` | list, read or cancel jobs |
| `WS /ws` | `{type:'job', job}`, `{type:'system', status}` (every 2 s) and `{type:'project', projectId, event}` |

---

## Development

```powershell
scripts\dev.ps1                 # API with --reload (new window) + Vite dev server with HMR on :5173
scripts\dev.ps1 -Fake           # same, without Blender

.venv\Scripts\python.exe -m pytest -q                          # full suite (Blender-free parts)
$env:BIS_REAL_BLENDER = '1'; .venv\Scripts\python.exe -m pytest -q tests/test_api_integration.py
                                                               # real Blender 5.0: tiny draft + export
.venv\Scripts\python.exe -m uvicorn bis.main:app --app-dir server --port 8420   # server only
```

**Server tests**

| File | What it tests |
|---|---|
| `tests/test_api.py` | REST, WebSocket, the job queue, coalescing, cancel and auto preview, using a FakeBridge and a fake `bis.svg` |
| `tests/test_api_bridge.py` | the real `BlenderBridge` against a stdlib stand-in process: READY handshake, progress, stdout draining, crash restart, startup watchdog and one-shot kill |
| `tests/test_export.py` | every export target, the `.icon` writer and the image helpers |
| `tests/test_api_integration.py` | the real SVG pipeline, plus opt-in real Blender runs |

**Environment variables**

| Variable | Meaning |
|---|---|
| `BIS_BLENDER` | path to `blender.exe` (default: Blender 5.0 under Program Files) |
| `BIS_PORT` | server port (default 8420) |
| `BIS_WORKSPACE` | data directory (default `<repo>/workspace`) |
| `BIS_NO_WORKER=1` | don't start the persistent worker at startup |
| `BIS_FAKE_BLENDER=1` | use the FakeBridge (no Blender, no GPU) |
| `BIS_EXPORT_MAX_SIZE` | cap export master renders (px), for debugging |

---

## GPU notes

- Cycles always uses the **OptiX** device. The CUDA and CPU device entries are disabled, and the **OptiX
  denoiser** is used. EEVEE drafts use the same GPU.
- The GPU is shared with the desktop: an 8 GB card already carries 3.5–4.7 GB from the desktop. To stay
  within that:
  - interactive tiers are clamped to **512 px**;
  - `use_persistent_data` is off;
  - Final (1024 px) and Ultra (2048 px) renders run in one-shot processes that release their VRAM on exit.
- The first EEVEE render after a driver or Blender update compiles shaders, which can take about 12 s. The
  worker warms up at startup, and the status bar shows `starting` until it is ready.
- Exports at Final quality render up to 13 master images, one Blender process each (about 2–3 s of
  Blender start-up per image on top of the render). The Ultra tier is for hero shots only.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Status bar says **Blender not found** | Install Blender 5.0, or set `BIS_BLENDER` to `blender.exe`, then restart. |
| Worker state **error** / renders fail | The status bar shows the last Blender message. Full output: `GET /api/system/logs` or `workspace/logs/blender.log`. Click *Restart worker* (`POST /api/system/worker/restart`). |
| First render is slow | NVIDIA shader-cache compile after a driver update (one time). |
| Out of VRAM (OptiX error) | Close other GPU-heavy apps, lower the size or tier, or render Ultra only when needed. |
| "Web UI not built" page | Run `Blender Icon Studio.cmd -Rebuild`, or `cd web; npm run build`. |
| Port 8420 is busy | `Blender Icon Studio.cmd -Port 8430` |
| Export fails with "path is too long for Windows" | Move the repo (or just the data: set `BIS_WORKSPACE`) to a shorter folder, or enable Win32 long paths (`LongPathsEnabled`). |
| Edge doesn't open | The launcher falls back to the default browser. You can also open <http://127.0.0.1:8420> yourself. |
| `.icon` won't open in Icon Composer | The format is community-documented, so the export is **beta**. Import the layer SVGs from `Assets/` manually. |

Server log: `workspace/logs/server.log`. Blender output: `workspace/logs/blender.log`.

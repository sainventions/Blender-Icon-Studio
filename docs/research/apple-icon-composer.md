# Apple Icon Composer: Research Notes for Blender Icon Studio

> **Critic note (2026-10-03):** see [`critic-review.md`](critic-review.md). It covers:
> - the mapping between Icon Composer groups and layers and the svg-pipeline layers and regions (C11);
> - recipes still missing for appearance renditions, blend modes and translucency falloff (§4);
> - the `.icon` schema, which is still unverified against a real 2.0 file (U1).

Researched 2026-10-03. This covers **Icon Composer 1.x** (WWDC25, June 2025, shipped with Xcode 26 / iOS 26) and **Icon Composer 2.0** (WWDC26 beta on 2026-06-12, shipped with Xcode 27 around 2026-09-14 for the "2027" OS releases: iOS/iPadOS/macOS/watchOS 27).
The current standalone download requires **macOS Tahoe 26.4 or later**. Icon Composer is macOS-only.

Confidence labels:
- **[Apple]**: Apple docs, HIG, WWDC transcripts, or the Icon Composer product page.
- **[Obs]**: seen directly in Apple's own screenshots (Icon Composer 1.x documentation images).
- **[3P]**: third-party reverse engineering or reviews.
- **[Unverified]**: plausible, but not confirmed.

---

## 0. TL;DR for product decisions

- Icon Composer is a **compositor, not a drawing tool**. You import flat SVG/PNG layers, put them in **at most 4 groups**, set a few coarse glass parameters on each group, and annotate the Default, Dark and Mono appearances. It saves a `.icon` package (a JSON manifest plus assets). The OS renders the glass live, and the look **changes between OS versions** (26 vs 27).
- The glass is a **2.5D screen-space effect**: there is no geometry, no real bevel profile, no IOR and no real lights. Its main parts are an edge specular rim, a frosted blur of what lies behind, translucency, a drop/ring shadow and (2.0) a refraction/lensing band near the edges.
- **The lighting is fixed.** In 1.x a preview-only light-angle dial was used (default −45°). In 2.0 there is a single "vertical light angle from above", and the gyro shimmer was removed in iOS 27.
- The biggest designer complaints:
  - too little control over specular highlights and lighting;
  - the 4-group limit;
  - no rotation, masks or richer gradients;
  - SVG import quirks;
  - preview vs device mismatches;
  - export limited to flattened PNG with no macOS margins;
  - Mac-only;
  - the macOS "squircle jail";
  - no animation or video export;
  - no non-Apple targets.
- **Our opportunity:** keep **data-model parity with `icon.json`** so we can read and write `.icon` bundles. Then go far beyond Apple with real extrusion and bevels, physically based glass in Cycles+OptiX (IOR, dispersion, caustics, thin film), free lighting and HDRIs, unlimited layers, animation export, and multi-platform export (Android adaptive, ICO, ICNS with correct margins, web), on Windows.

---

## 1. Complete feature inventory

### 1.1 Input formats and artwork preparation
- **SVG is preferred.** PNG and other raster formats are used for anything SVG can't express, such as mesh gradients, textures, photographic or pre-rendered elements, and "chromatic shadow" art [Apple]. The HIG also mentions PDF as a vector option [Apple, HIG]. One third-party tool lists the asset types a `.icon` bundle accepts as PNG, SVG, JPEG, WebP, HEIC and HEIF [3P].
- **Canvas sizes:**
  - **1024×1024 px** for iPhone, iPad and Mac (rounded rectangle);
  - **1088×1088 px** for Apple Watch (circle). The watch canvas "overshoots" the rounded rectangle so the same grid translates directly [Apple].
- **Export every layer at full canvas size** so its position is preserved. Number the files from back to front (they import alphabetically) [Apple].
- **Keep the art flat, opaque and simple.** Remove blurs, shadows, specular, opacity, translucency, background colors and gradients, and **don't export the mask**. Pre-masked layers "negatively impact specular highlight effects and make edges look jagged" [Apple/HIG].
- **SVG does not preserve fonts**, so convert text to outlines [Apple].
- **Prefer clearly defined edges**, and avoid soft or feathered edges on foreground shapes so system highlights and shadows work [HIG]. Prefer bold strokes, rounded corners and filled shapes over outlines [HIG/WWDC25-220].
- **Known SVG quirks** [3P]:
  - Invisible frame `<rect>`s exported by Affinity and Penpot are treated as real geometry. They break highlights, and the whole rect gets filled when a Solid fill override is applied.
  - Self-intersecting paths are flattened.
  - Some SVG effects are interpreted "opposite to intent".
- **Display P3:** the Document inspector has an "SVG Colors: Use Display P3 if untagged" toggle [Obs]. In JSON this is `color-space-for-untagged-svg-colors: srgb|display-p3`.

### 1.2 Document structure: canvas, groups and layers
- **Canvas (the document root).** Selecting the root row in the sidebar edits the **background fill**. The background is not an imported layer.
  - Fill options: Automatic, None, Solid, Gradient, System Light, System Dark.
  - Icon Composer 2 adds **Automatic Gradient**: you pick one brand color and it generates the gradient [Apple, WWDC26 lab].
  - Apple recommends the System Light/Dark gradients over pure white or black, and soft light-to-dark gradients "that harmonize with the direction of light" [WWDC25-220].
- **Groups.**
  - **Maximum of 4 groups** "to reduce complexity". Going past this produces a warning [Apple; 3P].
  - Groups are the depth planes the OS renders. They are drawn bottom to top in sidebar order.
  - **All Liquid Glass parameters live at group level.**
- **Layers.**
  - Each imported file becomes a layer. Dragging a folder in creates a group.
  - There is no documented per-group layer cap.
  - Layers carry color properties, a **glass on/off "Effects" toggle** and composition (visibility, position, scale).
- **Sidebar editing.**
  - Rename by double-click.
  - Drag to reorder or re-parent. Arrange > Bring Forward / Send Backward.
  - Hover the eye icon to hide or show.
  - The + menu at the bottom has New Image and New Group. The − button deletes.
  - Context menu: Copy Style / Paste Style.
  - Composition > Image > **Replace** swaps a layer's file [Apple].

### 1.3 Per-group Liquid Glass properties (Style inspector → "Liquid Glass" section)

| Property | IC 1.x | IC 2.0 | Default / range |
|---|---|---|---|
| **Mode** | Individual / Combined | same | Individual. *Individual* treats each layer as its own piece of glass. *Combined* treats the group's layers as one glass object (one shared rim around the union, no inner edges at overlaps) [Apple] |
| **Specular** | On/Off toggle. Turning it off removes "the slight blur to the background and a light highlight around the edges" | **Off / Automatic / Inside / Outside** (`specular-highlight-placement`). *Inside* is best when the foreground is darker than the background; *Outside* when the background is darker. *Automatic* decides from layer colors | On / Automatic. On OS versions before 27, Inside and Outside simply mean "on" [Apple] |
| **Blur** (frosting) | toggle + % | toggle + % (`blur-material`, 0-1, default 0.5) | 50% [Obs]. Frosts whatever is behind the group inside its shapes |
| **Refraction** | not available | **toggle + 2D pad** (drag a puck) plus % fields for **Height/Depth** (radius of the lensing band, "how rounded") and **Strength** ("how much artwork is drawn into the shape"); optional **inverse refraction** | 0-1 each. Has no effect before OS 27 [Apple; WWDC26 lab] |
| **Translucency** | toggle + % | toggle + % | 50% [Obs]. Described as "transparent at the bottom, retains color at top". Used less by default in 2.0 to get sharper icons [WWDC26 lab] |
| **Shadow** | **Neutral** (UI label sometimes "Natural") / **Chromatic** / Off, plus opacity % | same kinds, but rendered as **"ring shadows"** with more definition around edges | Neutral 50% [Obs]. JSON kinds: `neutral`, `layer-color` (chromatic), `none`, `automatic` [3P] |
| **Opacity** (group) | % | % | per appearance |
| **Blend mode** (group) | yes | yes | see list below |

- **Neutral shadow** is the default: subtle, works on any background, recommended for Dark and Mono.
- **Chromatic shadow** spills the layer's color onto what's below it. It is best for colored art on light backgrounds [WWDC25-361].
- **Default override scopes** [WWDC25-361]:
  - Specular, Shadow, Blur and Translucency apply to **all appearances** by default.
  - Opacity, Blend mode and Fill are **per-appearance** by default.
- Layer-level glass is only the **Effects** on/off toggle [Obs]. You can mix pre-rendered raster layers (opted out of glass) with glass vector layers [WWDC26 lab].

### 1.4 Per-layer properties
- **Color section:**
  - **Opacity** (%).
  - **Blend Mode**: Normal, Darken, Multiply, **Plus Darker**, Lighten, Screen, **Plus Lighter**, Overlay, Soft Light, Hard Light [3P schema; WWDC26 lab]. Apple says Plus Darker and Plus Lighter "mirror system glass math" and keep things vibrant. For example, Podcasts uses Plus Lighter on a dark background.
  - **Fill**: Automatic (taken from the file), None, Solid, Gradient. The gradient is **2 stops, linear**. Its from/to points can be dragged on the canvas, each stop has its own opacity %, and a swap arrow flips the stops [Apple]. Colors can be named system colors (`named:system-blue`, and so on) [3P].
- **Composition section:**
  - **Visible** toggle.
  - **Layout**: x pt, y pt, scale %. There is **no rotation, skew, flip or mask** [Obs; 3P].
  - **Image** pop-up with a Replace option.
  - Groups also have Layout (position and scale).
- **Editing conveniences:**
  - Drag on the canvas with alignment guides. 2.0 adds **snap-to guides/grid** and **selection annotations**.
  - Arrow-key nudge.
  - Command-click for multi-select, or drag a marquee. Esc clears the selection.
  - Arrange > Align / Distribute.
  - Numeric fields accept expressions (`35*3`, `*2`).
  - Copy/Paste an individual setting, a whole section, or a whole Style. 2.0 can also **copy and paste attributes between icon files**.
  - 2.0 adds a **2D control with a modifier key to lock one axis** [WWDC26 lab].

### 1.5 Appearance modes
- **Annotated by the designer:** Default (Light), Dark and Mono.
- **Generated by the OS from the Mono annotation:** Clear Light, Clear Dark, Tinted Light and Tinted Dark. That makes **6 renditions** in total for iOS, iPadOS and macOS [Apple/HIG].
  - ictool rendition names: `Default`, `Dark`, `ClearLight`, `ClearDark`, `TintedLight`, `TintedDark` [3P].
- **watchOS has no appearance variants.** It always uses the light look.
- **Mono rules** [WWDC25-361]:
  - Make at least one element white (the most recognizable part).
  - Map the other elements to grays.
  - Auto-conversion is based on luminance and is often poor. For example, green art turns too dark, and Mono looks darker on device than in the preview [3P].
  - Maximize dynamic range [WWDC26].
- **Tinted:**
  - Light tint puts the color "directly into the glass".
  - Dark tint colors the foreground [WWDC25-220].
  - Clear mode is "pinned glass": it ignores the iOS 27 user transparency slider [WWDC26 lab].
- **Mono preview options popover** [Obs]: Appearance Light/Dark radio, a **Tinted** toggle, a **hue slider** and a **tint intensity/saturation slider**.

### 1.6 Per-appearance and per-platform overrides
- Each inspector section header (Color, Liquid Glass, Composition) has a **scope pop-up**: *All*, or the currently selected appearance or platform.
  - With **All**, overrides show **nested under the property**. For example, "Blend Mode: Normal" has child rows "Dark: Darken" and "Mono: Lighten", each with an **×** to remove it [Obs].
  - To add an override, select the variant in the canvas, then use the property's **+** icon and choose "Vary for Default", "Vary for Dark", "Vary for iOS / macOS", and so on [Obs].
  - Color and Liquid Glass vary **by appearance**. Composition varies **by platform**, so geometry can differ per platform while the look stays the same [Apple].
- **JSON model** [3P schema]:
  - Every property `p` has an optional `p-specializations: [{appearance?, idiom?, localization?, value}]`.
  - `appearance` ∈ `base|light|dark|tinted`, where *tinted* is the Mono annotation.
  - `idiom` ∈ `square|iOS|macOS|watchOS` (2.0).
  - `localization` is a locale ID (2.0).
- **2.0 also adds** RTL **asset mirroring** (document `implicit-asset-mirroring`, plus per-layer/group `asset-mirroring.mirrorable`) and localized assets.

### 1.7 Platforms, shapes, sizes and grids

| Platform | Layout canvas | Final shape | In Icon Composer? | Appearances |
|---|---|---|---|---|
| iOS / iPadOS / macOS | 1024×1024 square | Rounded rectangle (continuous-curvature "squircle", rounder radius since 26, concentric with hardware) | Yes ("iOS, macOS", Shared by default; 2.0 can specialize iOS vs macOS) | 6 |
| watchOS | 1088×1088 square | Circle | Yes (toggle) | none |
| visionOS | 1024×1024, 2-3 layers | Circle, 3D | **No** (Xcode image stack) | - |
| tvOS | 800×480, 2-5 layers | Rounded rectangle, parallax | **No** (image stack) | - |

- The **Document inspector** has a platforms toggle: "iOS, macOS" [Shared ▾] [on/off] and "watchOS" [on/off]. It hides the controls for platforms you don't ship [Obs].
- **Color spaces:** sRGB, Gray Gamma 2.2, Display P3 [HIG]. JSON also allows `extended-srgb` and `extended-gray` values above 1.0, and 2.0 adds **HDR export** [3P; WWDC26].
- **macOS.**
  - Since Tahoe (26), every app icon must fill the squircle. Non-conforming icons are shrunk onto a gray tile, the "squircle jail".
  - The system or Xcode adds the transparent margin and drop shadow at build time. Icon Composer's PNG export does **not** add these margins [3P, Michael Tsai].
- Exact squircle and grid geometry should be taken from **Apple Design Resources templates** (Figma, Sketch, Illustrator, Photoshop). **TODO:** extract the path for our mask and grid overlay.

### 1.8 Lighting and light-angle preview
- **IC 1.x:**
  - The toolbar has a **"Lighting angle" dial** (shows −45° by default) that rotates the light direction for the preview. It **does not edit the icon** [Obs/Apple].
  - On an iOS 26 device, highlights moved with the **gyroscope**.
- **IC 2.0 / OS 27:**
  - "A new, **vertical light angle shines from above**, matching the surrounding interface" [Apple product page].
  - The gyro shimmer was removed in iOS 27. Highlights now sit **stationary at the top and bottom edges** and are "much subtler" [MacRumors].
  - The 2.0 docs no longer list the angle dial. The toolbar instead has **Effects 26 / 27** buttons to compare render generations, and an **Effects on/off** toggle [Apple].
- **There is no user lighting control in the file.** Lighting belongs to the OS.

### 1.9 Preview and simulation controls
These are toolbar capsules above the canvas. They change only the preview.
- **Background:** a color well or image toggle, plus a background-image pop-up with presets and **Add Background…** for your own wallpaper. This is used to judge Clear and Tinted modes.
- **Grid:** a toggle plus a Light/Dark grid color pop-up.
- **Lighting angle dial** (1.x only).
- **Preview size:** for example "1024pt 1x". The WWDC26 lab mentions a **"waterfall"** of all the common presentation sizes [Unverified UI detail].
- **Zoom %.**
- **Effects** (2.0): compare 26 vs 27 rendering, or turn glass off.
- **Bottom of the canvas:**
  - Bottom-left: the platform label and thumbnails (rounded square and circle).
  - Bottom-right: the appearance label and thumbnails (Default, Dark, Mono), plus the Mono Options popover.

### 1.10 The `.icon` file format
- It is a **macOS package (folder)**: `MyIcon.icon/icon.json` plus `MyIcon.icon/Assets/<files>` [3P].
- **Top-level keys** of `icon.json` (community schema, giginet/apple-icon-composer-skill):
  - `fill` / `fill-specializations`;
  - `groups[]`, which are ordered **front to back** (the first entry is drawn on top);
  - `supported-platforms {squares:"shared", circles:["watchOS"]}`;
  - `color-space-for-untagged-svg-colors`;
  - (2.0) `features[]`, `implicit-asset-mirroring` and `languages[]`.
- **group** keys:
  - `name` and `layers[]`;
  - `lighting` (`individual|combined`);
  - `specular` (bool);
  - (2.0) `specular-highlight-placement` (`automatic|inside|outside`);
  - `blur` (1.x) or `blur-material` (2.0, 0-1);
  - (2.0) `refractivity {enabled, strength 0-1, depth 0-1}`;
  - `translucency {enabled, value}`;
  - `shadow {kind, opacity}`;
  - `blend-mode`, `opacity`, `hidden`;
  - `position {scale, translation-in-points:[x,y]}`;
  - each of these can also have a `-specializations` sibling.
- **layer** keys:
  - `name` and `image-name` (or `image-name-specializations`, which allows per-appearance or per-platform artwork swaps);
  - `fill`, `blend-mode`, `opacity`;
  - `glass` (bool);
  - `hidden`, `position`;
  - (2.0) `asset-mirroring`;
  - plus `-specializations` siblings.
- **fill** is either a keyword (`automatic|none|system-light|system-dark`) or one of: `{solid}`, `{linear-gradient:[c1,c2], orientation:{start:{x,y}, stop:{x,y}}}` with normalized points, or (2.0) `{automatic-gradient: color}`.
- **Color strings** look like `"display-p3:1.0,0.188,0.181,1.0"`, `"extended-gray:1,1"` or `"named:system-blue"`.
- **Forward-compatibility gate:** a 2.0 file that uses refraction or specular placement declares `features:["refractivity","specular-location"]`. Icon Composer 1.x refuses to open it.
- **Caveat:** this schema is community reverse-engineered, not Apple-published. Validate any `.icon` writer against real Icon Composer files.

### 1.11 Export options
- **File > Save** writes the `.icon` package, which is the shipping artifact.
- **File > Export** writes a **flattened PNG** "for marketing and communication". You choose the platform, appearance and size/scale, one image per export [Apple; 3P].
  - It does **not** add the macOS transparent margin [3P].
  - 2.0 adds **HDR export** [WWDC26].
- **CLI:** `ictool` ships hidden inside `Icon Composer.app/Contents/Executables/ictool` and is undocumented [3P]. Examples:
  - `ictool AppIcon.icon --export-image --output-file out.png --platform iOS --rendition Default --width 1024 --height 1024 --scale 1`
  - legacy form: `ictool x.icon --export-preview macOS Light 256 256 1 out.png`
  - Third-party wrappers also expose light angle, tint color and strength, and a background.
  - Headless Clear renditions render against gray, because true glass transparency needs Metal [3P].
- There is **no** export to `.icns`, `.ico`, `.appiconset`, Android, web, video or GIF.

### 1.12 Xcode integration
- Launch from Xcode > Open Developer Tool > Icon Composer, or as a standalone download.
- In Xcode, selecting a `.icon` file shows a preview and an "Open with Icon Composer" option.
- **Adding an icon:**
  - Drag the `.icon` into the Project navigator as a **resource**. It goes next to the source files, **not inside `Assets.xcassets`**.
  - Make sure the target's General > App Icon name matches the filename without its extension.
  - Several `.icon` files can exist, but only the one whose name matches is used.
  - Alternate icons need "Include All App Icon Assets" and Info.plist entries. They are reliable from Xcode 27 [3P].
  - Xcode 27 adds File > New > File From Template > Icon Composer file [3P].
- **The `.icon` replaces the AppIcon asset catalog.** Xcode's `actool` **auto-generates flattened fallback PNGs** for older OS deployment targets. Complaint: this adds about 2.8 MB of 1024² PNGs [Apple; forums].
- **No recompile is needed for 2027 rendering.** Existing 26-era icons automatically get the new, sharper material on OS 27. The reduced-translucency changes are not automatic [WWDC26 lab].

---

## 2. UI layout (for designing a similar but better UI)

Icon Composer 1.x layout, based on Apple's annotated screenshot; 2.0 differences are noted.

```
┌───────────────┬───────────────────────────────────────────────┬──────────────────────┐
│ ● ● ●     [▥] │ Landmarks App Icon.icon   [bg|img] [#|▾] [◜ -45°] │   [Style][Doc]   │
│               │  Edited    (bg)   (grid)  (light) [1024pt 1x▾] │──────────────────────│
│ ▢ Landmarks…  │                                     [100%▾]   │ Color        Default▾│
│ ▾ [G] Group    │                                               │  Opacity      100 %  │
│    ▦ layer-5  │                                               │  Blend Mode  Normal ▾│
│ ▾ [G] Group    │             ┌───────────────────┐             │  Fill      Gradient ▾│
│    ▦ layer-4  │             │                   │             │   [■ from][▾]        │
│    ▦ layer-3  │             │   live icon       │             │   [■ to  ][▾]        │
│    ▦ layer-2  │             │   preview         │             │ Liquid Glass Default▾│
│ ▾ [G] Group ◀  │             │                   │             │  Mode    Individual ▾│
│    ▦ layer-1  │             └───────────────────┘             │  Specular      [on]  │
│               │                                               │  Blur     [on] 50 %  │
│               │  iOS, macOS                         Default   │  Translucency[on]50% │
│               │  [▢][◯]  ← platform    appearance → [Def][Drk][Mono]│  Shadow Neutral▾ 50% │
│ [+] [−]       │                                               │ Composition     All▾ │
│               │                                               │  Visible       [on]  │
│               │                                               │  Layout x 0pt y 0pt  │
│               │                                               │         ⤢ 100 %      │
└───────────────┴───────────────────────────────────────────────┴──────────────────────┘
```

- **Left sidebar** (collapsible):
  - A document root row. Select it to edit the background fill.
  - Groups as folders with disclosure triangles. Layers with checkerboard thumbnails.
  - The eye icon appears on hover.
  - + / − buttons at the bottom.
  - Drag-and-drop target for files and folders.
- **Canvas** (center):
  - The filename and "Edited" status at top-left.
  - Toolbar capsules along the top. 1.x has **background, grid, light angle, preview size, zoom**. 2.0 replaces the light dial with **Effects 26/27 + on/off** and adds snapping guides.
  - Platform switcher at bottom-left, appearance switcher at bottom-right. Each is a row of live mini-thumbnails with a floating label.
  - The canvas is a large single preview. There is no multi-variant grid view.
  - In Clear and Tinted modes, the chosen wallpaper fills the whole canvas, and the icon's glass refracts and frosts it.
- **Inspector** (right) has two tabs:
  - **Style** (paintbrush; called the "Appearance inspector" in 1.x), with sections Color, Liquid Glass and Composition. Each section has a scope pop-up (All / Default / Dark / Mono / platform).
  - **Document**: Platforms and SVG color space.
  - Rows use a consistent icon + label + control layout: toggles plus a % field for Blur and Translucency, and pop-up plus % for Shadow.
  - Overrides are shown as nested rows with an × and a + "Vary for…" menu.
- **Popovers:** Mono Options (Light/Dark, Tinted toggle, hue and intensity sliders).

**UI lessons for us:**
- Apple's single big preview forces you to toggle variants one at a time. We should offer a **variant matrix**: every appearance × platform × several sizes at once. This is a "waterfall" plus contact sheet.
- Apple's override model is good and should be copied: scope pop-up, nested override rows, "Vary for…".
- Its value ranges are too coarse: binary toggles and 0-100% with no units. We should expose real physical units (mm or px depth, IOR, roughness, light angle and elevation) behind "simple" presets.
- Apple shows no 3D or exploded view of the stack. Our three.js viewport can offer a **tilt/orbit "exploded" view** of the layer stack, a direct win.

---

## 3. Visual characteristics of Liquid Glass on icons

### 3.1 Apple's own description
- "The material layers different elements like **edge highlights, frostiness, and translucency** to not only add a sense of depth, but… makes it seem as if the icons are **lit from within**" [WWDC25-220].
- On a 26-era Home Screen, **specular highlights respond to gyro motion** [WWDC25-220].
- Liquid Glass in general "dynamically **bends, shapes, and concentrates light**" (lensing), while older materials "scattered light" [WWDC25-219].
  - Highlights "respond to geometry" and travel around the silhouette when lights move.
  - Shadows deepen when the glass is over busy content.
  - Nearby colorful light "spills onto its surface… and bleeds into the shadow".
  - Larger glass "simulates a thicker material" with more pronounced lensing and softer scattering.
  - Colored glass "changes hue, brightness and saturation depending on what's behind" [WWDC25-219].

### 3.2 Per-effect breakdown
Compiled from the Apple docs, the WWDC sessions and Apple's 1.x screenshots [Obs].

1. **Edge specular rim.**
   - A thin, bright, slightly blurred highlight runs along the shape's edge, strongest on the edges facing the light. With the 1.x default of −45° that is the top-left, with a weaker counter-highlight at the bottom-right.
   - It behaves like a rounded bevel catching light. A soft inner glow makes the shape look pillowy, and Apple warns it can get "too pillowy" on thin shapes.
   - The **canvas squircle itself** is a thick glass slab with its own bright rim.
   - **2.0** makes the rim **crisper** and adds **darker edge detail**, a thin dark outline on the shadowed side that "helps describe the shape". You can choose *Inside*, *Outside* or *Automatic* placement relative to the art's edge. Light now comes vertically from above, with highlights at the top and bottom.
2. **Frosting (Blur).**
   - Content behind a glass layer is blurred within that layer's shape: lower groups, the background fill, and the wallpaper in Clear mode.
   - In 1.x, turning Specular off also removed "the slight blur to the background".
3. **Translucency.**
   - The layer becomes partly transparent so that what's behind shows through. Apple describes it as "transparent at the bottom, retains color at the top", a vertical opacity falloff.
   - In Clear modes the foreground becomes whitish frosted glass, and the wallpaper is visible through both the foreground and the container.
4. **Refraction / lensing.**
   - In 1.x this is implicit and mild. Edge lensing of the wallpaper is visible at the container rim in Clear mode, and the inside of the icon looks offset from the outside.
   - In **2.0** it is explicit per group: **Height/Depth** sets the width of the lensing band, **Strength** sets how far content is pulled in, and **Inverse** bends the opposite way. Layers "pick up and transmit color and shape from what's behind them". The range runs "from a subtle edge bend to lens-like distortion".
5. **Layered depth.**
   - Up to 4 groups act as stacked glass planes, each casting a shadow onto the planes below.
   - *Individual* mode gives every layer its own rim, so overlaps show a second edge. *Combined* fuses a group into one object with a single rim around the union.
6. **Shadows.**
   - **Neutral**: a soft gray drop shadow offset downward, consistent with top lighting.
   - **Chromatic**: the shadow takes the layer's own hue, like colored light passing through glass.
   - **2.0 "ring shadows"**: a tighter shadow that hugs the edges for crisper separation.
7. **Tint.** Tinted Light puts color into the glass body. Tinted Dark colors the foreground on a dark glass base. Mono maps everything to luminance.
8. **What 2.0 / OS 27 changed overall.**
   - Glass is now "a refined finish rather than a dominant overlay".
   - The result has **higher contrast, sharper edges, narrower bevels, thin dark outlines and less translucency**.
   - There is **no gyro shimmer**.
   - Refraction is applied selectively rather than everywhere [MacRumors; Wikipedia; WWDC26].

### 3.3 Implications for our renderer
To be "Apple-like", our glass shader or material needs:
- a rounded-edge (bevel) normal profile with a controllable radius, which drives the rim specular and the edge refraction;
- frosted transmission (roughness);
- vertical translucency falloff;
- per-layer drop or ring shadow, with neutral or chromatic tint;
- the canvas slab as glass with its own rim;
- Individual vs Combined (the union of shapes before beveling);
- Inside vs Outside highlight placement;
- light presets: "Apple 26" (−45°, softer) and "Apple 27" (top-down vertical, crisp, dark rim).

In Blender this maps naturally to real extruded and beveled curves with a Glass or Principled transmission material, a light rig plus HDRI, and a shadow catcher. The three.js live preview can approximate it with `MeshPhysicalMaterial` (transmission, thickness, ior, roughness, dispersion, iridescence, clearcoat) [Unverified in detail; check our three.js version].

---

## 4. Limitations and complaints (where a Blender-powered tool can win)

**Tool and platform**
1. **macOS-only**, and it needs a recent macOS (Tahoe 26.4+). There is nothing for Windows or Linux users, or designers on PCs.
2. **Apple-only output.** visionOS and tvOS aren't supported even inside Apple's ecosystem (forum complaint), and there is nothing for Android, Windows, web/PWA or favicons.
3. **It is not a design tool.** There is no vector drawing, booleans or text, so it depends on an external editor. Users call it "very basic", with "janky pieces"; resizing and positioning layers is "a hassle" (Basic Apple Guy).
4. **Fragile SVG import:** invisible frame rects, self-intersecting paths, no fonts, effects misread. PNG is often recommended as a workaround, which brings scaling and aliasing problems.
5. **Layout limits:** the window's minimum size is too large for 13" MacBook resolutions (forums), and there is a single big preview with no multi-variant view.

**Composition**
6. **At most 4 groups.** Developers had to redesign icons to fit (fatbobman, simplykyra). Complex "weaving" needs slicing tricks (WWDC26).
7. **No rotation, skew, flip, masks, clipping or per-layer effects.** Glass is either fully on or off per layer, and every other parameter is per group.
8. **Fills are basic:** solid, 2-stop linear gradient, or automatic gradient. There are no radial, angular or mesh gradients, no multi-stop gradients, and no noise, grain or texture.
9. **Blur conflicts with glass:** you can't have a custom aesthetic blur and the glass style at the same time (fatbobman).

**Material and light (the core gap)**
10. **Specular control is minimal:** off/auto/inside/outside, with no intensity, width, color, falloff or roughness. Louie Mantia: "For every Liquid Glass app icon I make, I wish I had way more control over the specular highlights." Designers end up baking their own lighting into the art.
11. **Lighting is fixed:** a preview-only dial in 1.x, a fixed top light in 2.0. There are no colored lights, multiple lights or HDRI environments.
12. **No physically based glass:** no IOR, thickness, bevel profile, absorption/color depth, dispersion, caustics, thin-film iridescence, metal, clay, plastic or emissive materials. The HIG explicitly steers designers away from "realistic 3D objects", so a real 3D look is impossible inside the tool.
13. **No real 3D:** no extrusion depth, bevel shape, perspective or camera.
14. **Shadows** are neutral or chromatic plus opacity only, with no direction, softness or distance controls.

**Consistency and output**
15. **The look is owned by the OS and changes between releases.** Icons re-render differently on 26 vs 27 (an advantage for Apple, a loss of control for designers). The preview often differs from devices and the App Store: Mono is darker on device, tinted variants look terrible on iOS 18 fallbacks, and the App Store shows a square tile.
16. **Mono/tinted generation is naive** (luminance-based), so manual gray mapping is required.
17. **Export is weak:**
    - a single flattened PNG per export;
    - no macOS margins or shadow;
    - no ICNS, ICO, iconset or appiconset;
    - the hidden CLI (`ictool`) is undocumented;
    - headless Clear renders show flat gray;
    - early versions had alpha-channel upload errors and black borders on PNG exports.
18. **No animation:** no turntable, light sweep, parallax video, GIF/APNG/WebM or Lottie. The WWDC26 lab left these questions unanswered, and 2.0 removed the gyro motion entirely.
19. **Xcode friction** (mostly fixed by 27):
    - alternate icons;
    - CI failures in `actool`;
    - `NSImage(named:)` and `UIImage(named:)` can't load `.icon`;
    - the 2.8 MB of fallback PNGs;
    - no way to ship a different legacy icon for older macOS.
20. **macOS "squircle jail"** (Tahoe and Golden Gate/27): no custom silhouettes. Critics (Siracusa, Gruber, Rogue Amoeba) say it harms recognizability. Apple's redesigned first-party icons were called "blurry, dumbed-down" with a "loss of detail" (One Foot Tsunami, Rogue Amoeba).

---

## 5. Parity features (must match) and beyond-Apple features (should add)

### 5.1 Parity: must match

**Document and model**
- [ ] Data model isomorphic to `icon.json`: background fill; groups (ordered) → layers; per-property **specializations** keyed by appearance (base/light/dark/tinted) and idiom (iOS/macOS/watchOS), with localization optional.
- [ ] **Import and export `.icon` packages** (round-trip) so users can still ship native Liquid Glass through Xcode. Write `features[]` correctly for refraction and specular placement.
- [ ] SVG and PNG layer import at full canvas size, with drag-drop of files and folders (folder → group, alphabetical ordering by numbered names).
- [ ] Canvas 1024² (rounded rectangle) and 1088² (watch circle), with the Apple grid overlay (light/dark) and the exact squircle mask from Apple Design Resources.
- [ ] Display P3 / sRGB handling, including the "untagged SVG as P3" option.

**Per-layer controls**
- [ ] Opacity.
- [ ] Blend modes: normal, darken, multiply, plus-darker, lighten, screen, plus-lighter, overlay, soft-light, hard-light.
- [ ] Fill: automatic, none, solid, 2-stop linear gradient with on-canvas handles, system-light/dark, automatic-gradient.
- [ ] Glass on/off, visibility, x/y/scale, replace image.

**Per-group glass controls**
- [ ] Mode Individual/Combined.
- [ ] Specular Off/Auto/Inside/Outside.
- [ ] Blur %.
- [ ] Refraction on/off + strength + depth (+ inverse).
- [ ] Translucency on/off + %.
- [ ] Shadow neutral/chromatic/off + opacity (+ ring style).
- [ ] Group opacity, blend mode and position.

**Appearances**
- [ ] Default, Dark and Mono annotations, plus generated Clear Light/Dark and Tinted Light/Dark previews, with tint hue and intensity controls.

**Override UX**
- [ ] Scope pop-up per section; nested override rows with ×; "Vary for <appearance/platform>".

**Preview**
- [ ] Background color or custom wallpaper image.
- [ ] Grid.
- [ ] Preview size and zoom.
- [ ] Effects on/off.
- [ ] Light angle.
- [ ] "Apple 26 look" vs "Apple 27 look" comparison presets.

**Editing**
- [ ] Undo/redo.
- [ ] Copy/paste style, sections and settings (also across files).
- [ ] Snapping guides.
- [ ] Align/distribute.
- [ ] Arrow-key nudge.
- [ ] Expressions in numeric fields.
- [ ] Multi-select.
- [ ] Rename and reorder.

**Export**
- [ ] Flattened PNG per platform × appearance × size/scale (including an HDR option).
- [ ] An **"export all renditions"** batch, which Apple lacks in its GUI.
- [ ] Group limit: allow more than 4, but show a **"Apple .icon compatible (≤4 groups)"** warning badge.

### 5.2 Beyond Apple: should add (ordered roughly by impact for our stack)

**3D and materials (Blender's core strengths)**
1. **Real geometry.**
   - SVG → curves → **extrude + bevel** per layer, with a bevel profile (round, chamfer, custom curve) and per-layer depth and Z spacing.
   - Rounded "pillow" and inflate modes.
   - Booleans and unions to implement *Combined* physically.
2. **Physically based glass in Cycles with OptiX** on the RTX 3070 Ti, using the OptiX denoiser: IOR, roughness/frost, absorption (volume color for thickness-dependent tint), **dispersion**, **thin-film iridescence**, and **shadow caustics**. EEVEE is used for fast drafts. Verify feature availability in Blender 5.0.
3. **Material library beyond glass:** frosted acrylic, clear-coat plastic, brushed and polished metal, clay/matte, ceramic, emissive/neon, subsurface "gummy", holographic. Mixable per layer, with presets such as "Apple 26 Glass", "Apple 27 Glass", "Vision Pro", "Frutiger Aero".
4. **Full lighting control:**
   - light angle **and** elevation, multiple lights, colored rim lights, area-light softness;
   - **HDRI environments** (studio, outdoor, custom);
   - per-appearance light rigs;
   - a fully controllable specular, the #1 designer complaint: intensity, width, color, placement.
5. **Better shadows:** direction, softness, distance, contact/ambient occlusion, colored and caustic shadows, plus a shadow-catcher pass for transparent output.

**Composition freedom**
6. **Unlimited groups and layers.**
7. **Rotation, skew and flip; masks and clipping; per-layer effects** (inner glow, noise, grain).
8. **Rich fills:** multi-stop linear, radial, angular and mesh gradients; noise; image textures.
9. **Semi-automatic SVG layer splitting** (our core differentiator): split by color, path groups or connected shapes; auto-assign depth; clean invisible frame rects; fix self-intersections; handle text-to-outline.
10. **Custom silhouettes** for platforms that allow them (Android adaptive, web, Windows, macOS pre-Tahoe), so users aren't stuck in squircle jail where it's optional.

**Preview and UX**
11. **Variant matrix and contact sheet:** every appearance × platform × size (a waterfall from 16 px to 1024 px) on one screen, plus **home-screen and Dock mockups** with real wallpapers.
12. **Exploded 3D view** of the layer stack, orbit and tilt, in the three.js viewport.
13. **Live interactive tilt** to simulate the iOS 26 gyro shimmer and parallax, and to scrub the light angle.
14. **Smarter Mono/Tint generation:** a perceptual luminance remap with a target dynamic range, a "make primary element white" helper, and contrast and legibility warnings at small sizes.
15. **Style presets, batch and icon families:** apply one look to a whole icon set (toolbar icons, product families). A/B snapshots and version history.

**Output**
16. **Animation export:** turntable, light sweep, glint pass, parallax tilt, and layer build-in/explode, as MP4, WebM, GIF, APNG or image sequences (and possibly Lottie for 2D parts). These are useful for marketing and app previews.
17. **Multi-platform export:**
    - Apple `.icon` and appiconset;
    - macOS **ICNS with correct Tahoe margins and shadow**;
    - **Android adaptive icons** (108 dp foreground/background/monochrome layers);
    - Windows ICO/MSIX;
    - favicons and PWA maskable icons;
    - App Store / Play Store marketing PNGs;
    - 16-bit PNG, EXR and HDR;
    - render passes (alpha, shadow, cryptomatte) for compositing.
18. **Scriptable CLI and HTTP API** (the documented equivalent of `ictool`) for CI and agents, plus project files that are diff-friendly JSON.
19. **Cross-platform:** runs on Windows (our target) and anywhere a browser and Blender run.
20. **Locked look:** renders are deterministic and owned by the designer, not re-rendered differently by each OS release. An "emulate Apple OS 26/27" preset keeps parity when needed.

---

## 6. Open questions / to verify
- The exact squircle path and grid for 26/27: extract from the Apple Design Resources templates (Figma or Sketch).
- How *inverse* refraction is encoded in `icon.json`: the schema only shows strength and depth in the range 0-1. Inspect a real 2.0 file.
- The exact Icon Composer 2 Export dialog fields: platform, rendition, size, scale, HDR, and whether there is a batch "all" option.
- The full `ictool` flags in 2.0, for example light angle and tint. The current knowledge comes from third-party wrappers.
- Whether the 2.0 preview-size control is literally a "waterfall" view, and whether any light-angle preview remains in 2.0.

---

## Sources
- Apple. [Creating your app icon using Icon Composer](https://developer.apple.com/documentation/xcode/creating-your-app-icon-using-icon-composer) (current 2.0 text). The 1.x text and screenshots are mirrored at [livingston/apple-docs](https://github.com/livingston/apple-docs/blob/main/documentation/Xcode/creating-your-app-icon-using-icon-composer.md).
- Apple. [Icon Composer product page](https://developer.apple.com/icon-composer/).
- Apple HIG. [App icons](https://developer.apple.com/design/human-interface-guidelines/app-icons), change log "June 8, 2026: Refined guidance for Liquid Glass".
- WWDC25. [Create icons with Icon Composer (361)](https://developer.apple.com/videos/play/wwdc2025/361/) · [Say hello to the new look of app icons (220)](https://developer.apple.com/videos/play/wwdc2025/220/) · [Meet Liquid Glass (219)](https://developer.apple.com/videos/play/wwdc2025/219/) · [WWDC Notes 361](https://wwdcnotes.com/documentation/wwdc25-361-create-icons-with-icon-composer/).
- WWDC26. [Icon Composer for Beginners Group Lab (8012)](https://developer.apple.com/videos/play/wwdc2026/8012/) · [Anton Gubarenko Q&A notes](https://antongubarenko.substack.com/p/wwdc26-icon-composer-for-beginners) · [kruschel.dev notes](https://kruschel.dev/notes/wwdc26-group-labs/wwdc2026-8012).
- 9to5Mac. [Icon Composer 2 and SF Symbols 8 betas](https://9to5mac.com/2026/06/12/icon-composer-2-and-sf-symbols-8-now-available-as-betas/) · [Memeburn](https://memeburn.com/icon-composer-2-and-sf-symbols-8-beta-what-apples-new-developer-tools-mean-for-liquid-glass-app-design/).
- MacRumors. [iOS 27 revamps app icons](https://www.macrumors.com/2026/06/16/ios-27-revamps-app-icons/) · [Liquid Glass changes in iOS 27](https://www.macrumors.com/2026/06/10/how-liquid-glass-is-changing-in-ios-27/) · [Wikipedia: Liquid Glass](https://en.wikipedia.org/wiki/Liquid_Glass).
- `.icon` schema: [giginet/apple-icon-composer-skill](https://github.com/giginet/apple-icon-composer-skill) (icon-schema.json) · [ethbak/icon-composer-mcp](https://github.com/ethbak/icon-composer-mcp).
- ictool and export: [John Brayton notes](https://www.virtualsanity.com/202507/icon-composer-notes/) · [Michael Tsai: Mac .icon margins](https://mjtsai.com/blog/2025/10/02/how-to-export-a-mac-icon-file-with-the-proper-margins/) · [Use Your Loaf](https://useyourloaf.com/blog/adding-icon-composer-icons-to-xcode/).
- Complaints:
  - [fatbobman: Tackling Challenges](https://fatbobman.com/en/posts/icon-composer-tackling-challenges/)
  - [jrogel review](https://jrogel.com/apples-icon-composer-promising-impressive-but-not-quite-ready-for-prime-time/)
  - [Simply Kyra](https://www.simplykyra.com/blog/sketching-composing-and-failing-my-app-icon-experience-with-apples-new-tool-icon-composer/)
  - [Basic Apple Guy](https://basicappleguy.com/basicappleblog/icon-composer)
  - [Michael Tsai: Icon Composer Notes](https://mjtsai.com/blog/2025/06/23/icon-composer-notes/)
  - [Apple Developer Forums tag](https://developer.apple.com/forums/tags/icon-composer)
  - [Penpot SVG issue #11916](https://github.com/penpot/penpot/issues/11916)
  - [One Foot Tsunami: Tahoe's Terrible Icons](https://onefoottsunami.com/2025/11/05/tahoes-terrible-icons/)
  - [Daring Fireball: Squircle Jail](https://daringfireball.net/2026/07/eliminate_app_icon_squircle_jail)
  - [Rogue Amoeba: Free the Icons](https://weblog.rogueamoeba.com/2026/06/26/free-the-icons/)
  - [MacStories macOS 27 review](https://www.macstories.net/stories/macos-27-the-macstories-review/2/)
- Web clone reference (WebGL2 glass: separable blur + refraction + Fresnel rim + dispersion): [frontedu/liquid-composer](https://github.com/frontedu/liquid-composer).

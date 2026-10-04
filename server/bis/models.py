"""Shared data contract for Blender Icon Studio.

This module is THE contract between the SVG pipeline, the server, the Blender worker and the web UI
(mirrored by hand in web/src/types.ts). Field names are camelCase on purpose so that the Python
attribute names are identical to the JSON keys (no alias handling anywhere).

Coordinate spaces (see docs/PLAN.md §3):
  * SVG space  — original viewBox units, y down.
  * Art space  — origin at viewBox centre, y UP, longer viewBox side spans 2.0 (−1..1).
  * Canvas     — the icon plate spans −1..1 (x, y). Artwork root = canvas.art (scale, x, y).
                 Blender: XY plane, camera on +Z looking −Z, 1 BU = 1 art unit.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

Vec2 = tuple[float, float]
BBox = tuple[float, float, float, float]  # minx, miny, maxx, maxy
ColorHex = str  # '#rrggbb'


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=False)


# ----------------------------------------------------------------------------------------------
# Paint / fills
# ----------------------------------------------------------------------------------------------
class GradientStop(_Model):
    offset: float  # 0..1
    color: ColorHex
    opacity: float = 1.0


class FillAuto(_Model):
    """Use the colours of the SVG art itself (rasterised layer texture)."""
    type: Literal["auto"] = "auto"


class FillNone(_Model):
    type: Literal["none"] = "none"


class FillSolid(_Model):
    type: Literal["solid"] = "solid"
    color: ColorHex = "#ffffff"
    opacity: float = 1.0


class FillLinear(_Model):
    """Linear gradient. start/end are in ART space (−1..1, y up)."""
    type: Literal["linear"] = "linear"
    stops: list[GradientStop]
    start: Vec2 = (0.0, 1.0)
    end: Vec2 = (0.0, -1.0)


class FillRadial(_Model):
    """Radial gradient in ART space. Optional 2x3 affine `matrix` maps gradient space -> art space
    (for elliptical/skewed SVG gradients); when present, center/radius are in gradient space."""
    type: Literal["radial"] = "radial"
    stops: list[GradientStop]
    center: Vec2 = (0.0, 0.0)
    radius: float = 1.0
    focal: Optional[Vec2] = None
    matrix: Optional[tuple[float, float, float, float, float, float]] = None


class FillSystem(_Model):
    """Apple 'System Light' / 'System Dark' background gradients."""
    type: Literal["system-light", "system-dark"]


Fill = Annotated[
    Union[FillAuto, FillNone, FillSolid, FillLinear, FillRadial, FillSystem],
    Field(discriminator="type"),
]
# Paint of an SVG element: what the art actually uses (never 'auto').
Paint = Annotated[Union[FillNone, FillSolid, FillLinear, FillRadial], Field(discriminator="type")]


# ----------------------------------------------------------------------------------------------
# Elements (shapes found in the SVG) — owned by the SVG pipeline
# ----------------------------------------------------------------------------------------------
class DropShadow(_Model):
    """Drop shadow parsed from an SVG <filter> (feOffset/feGaussianBlur/feFlood). Units: art space."""
    dx: float = 0.0
    dy: float = 0.0
    blur: float = 0.0
    opacity: float = 0.3
    color: ColorHex = "#000000"


class Element(_Model):
    id: str                       # stable within a project: 'e0', 'e1', ... ('img0' for rasters)
    name: str
    kind: Literal["path", "image"] = "path"
    paint: Paint                  # original paint (gradients in ART space)
    opacity: float = 1.0
    bbox: BBox                    # ART space
    area: float                   # fraction of the art square (0..1)
    groupPath: list[str] = []     # ancestor group names, outermost first
    origId: Optional[str] = None
    shadow: Optional[DropShadow] = None
    wasStroke: bool = False
    role: Literal["fill", "stroke", "image"] = "fill"


# ----------------------------------------------------------------------------------------------
# Materials / layers
# ----------------------------------------------------------------------------------------------
class MaterialSpec(_Model):
    """ONE Principled BSDF per shape. `preset` = starting values (shared/presets.json "materials");
    `params` = overrides keyed by the Principled schema (roughness, ior, transmission, coatWeight, ... +
    Paint pre-processing: paintMode, tint, grain, grainScale, filmVariation)."""
    preset: str = "liquid_glass"              # key into shared/presets.json "materials"
    params: dict[str, Any] = {}               # overrides of preset param defaults


class LayerTransform(_Model):
    x: float = 0.0       # art units, applied after canvas.art
    y: float = 0.0
    scale: float = 1.0


class LayerDepth(_Model):
    z: float = 0.0              # back face of the layer above the plate's front face (art units)
    thickness: float = 0.10     # total extrusion thickness
    bevel: float = 0.045        # round-edge radius (roundness); clamped to thickness/2 — height-field bodies taper thin parts
    bevelSegments: int = 6
    inflate: float = 0.0        # 0..1 dome on the front face (reflections sweep across flat faces)


class LayerShadow(_Model):
    # physical = the body really blocks/attenuates light (no shadow-ray trick; Cycles' true result);
    # neutral / chromatic = art-directed soft shadow (Is-Shadow-Ray transparent wrap, grey / tinted)
    # single-Principled rule: only real shadows are rendered; neutral/chromatic are legacy values rendered as physical
    kind: Literal["none", "physical", "neutral", "chromatic"] = "physical"
    opacity: float = 0.5        # 0..1 (Icon Composer shadow %)


BlendMode = Literal[
    "normal", "multiply", "screen", "overlay", "darken", "lighten",
    "soft-light", "hard-light", "plus-darker", "plus-lighter",
]


class Layer(_Model):
    """A depth plane (≈ Icon Composer *group*). Contains elements (≈ Icon Composer *layers*)."""
    id: str
    name: str
    elementIds: list[str]                       # z-order bottom -> top
    visible: bool = True
    locked: bool = False
    mode: Literal["individual", "combined"] = "individual"   # pieces vs. one union silhouette body
    fill: Fill = FillAuto()                     # 'auto' = SVG colours
    opacity: float = 1.0
    blendMode: BlendMode = "normal"
    glass: bool = True                          # Icon Composer per-layer 'Effects' toggle; False = flat inlay
    transform: LayerTransform = LayerTransform()
    depth: LayerDepth = LayerDepth()
    material: MaterialSpec = MaterialSpec()
    elementMaterials: dict[str, MaterialSpec] = {}   # per-shape overrides (element id -> material), merged over `material`
    shadow: LayerShadow = LayerShadow()


# ----------------------------------------------------------------------------------------------
# Document
# ----------------------------------------------------------------------------------------------
Platform = Literal["ios", "macos", "watchos", "android", "windows", "web", "free"]
PlateShape = Literal["squircle", "circle", "rounded", "square", "none"]


class Plate(_Model):
    visible: bool = True
    fill: Fill = FillSolid(color="#ffffff")
    material: MaterialSpec = MaterialSpec(preset="satin")
    thickness: float = 0.16
    bevel: float = 0.04


class ArtTransform(_Model):
    scale: float = 1.0
    x: float = 0.0
    y: float = 0.0


class Canvas(_Model):
    platform: Platform = "ios"
    shape: PlateShape = "squircle"
    cornerRadius: float = 0.225          # for 'rounded' (fraction of plate size)
    plate: Plate = Plate()
    art: ArtTransform = ArtTransform()


class Lighting(_Model):
    preset: str = "studio"               # key into presets.json "lighting"
    angle: float = -45.0                 # degrees; 0 = light from top, positive = clockwise (toward +x)
    elevation: float = 50.0              # degrees from the view axis (0 = from camera, 90 = grazing)
    intensity: float = 1.0
    rim: float = 1.0
    fill: float = 1.0
    environment: float = 1.0
    shadowSoftness: float = 0.5          # 0 = hard, 1 = very soft (light size)


class CameraSpec(_Model):
    view: Literal["front", "perspective"] = "front"
    tiltX: float = 0.0                   # degrees (perspective view): pitch
    tiltY: float = 0.0                   # degrees: yaw
    fov: float = 30.0                    # degrees (perspective)
    zoom: float = 1.0
    iso: float = 0.0                     # CAD-style POV: 0 = head-on (front) .. 1 = isometric; REAL distances, orthographic
    explode: float = 1.0                 # legacy z-gap multiplier — keep 1.0 (the UI uses `iso` instead)


class LayerOverride(_Model):
    fill: Optional[Fill] = None
    opacity: Optional[float] = None
    visible: Optional[bool] = None
    blendMode: Optional[BlendMode] = None
    material: Optional[MaterialSpec] = None


class AppearanceOverride(_Model):
    plateFill: Optional[Fill] = None
    layers: dict[str, LayerOverride] = {}   # keyed by layer id


class Tint(_Model):
    color: ColorHex = "#3b82f6"
    strength: float = 0.8


class Appearances(_Model):
    """Designer-annotated variants (Icon Composer: Default / Dark / Mono). The six renditions are
    derived: light = base; dark = base + dark; clear-* = base + mono rendered as clear glass;
    tinted-* = base + mono × tint."""
    dark: AppearanceOverride = AppearanceOverride(plateFill=FillSystem(type="system-dark"))
    mono: AppearanceOverride = AppearanceOverride()
    tint: Tint = Tint()


AppearanceId = Literal["light", "dark", "clear-light", "clear-dark", "tinted-light", "tinted-dark"]
Quality = Literal["draft", "preview", "final", "ultra"]


class RenderSettings(_Model):
    quality: Quality = "draft"
    size: Optional[int] = None           # None = tier default
    colorMode: Literal["brand", "neutral", "standard", "agx", "agx-punchy"] = "brand"   # brand = Standard + highlight soft-clip
    backdrop: Literal["transparent", "color", "wallpaper"] = "transparent"
    backdropColor: ColorHex = "#1c1c22"
    autoPreview: bool = True             # UI: auto Cycles preview after edits settle


class SourceInfo(_Model):
    filename: str
    viewBox: tuple[float, float, float, float]
    warnings: list[str] = []
    plateDetected: bool = False
    fullBleed: bool = False              # art itself forms the icon shape (no separate plate in the source)


SplitStrategy = Literal["smart", "group", "color", "element", "single"]


class Project(_Model):
    version: int = 1
    id: str
    name: str
    createdAt: str
    updatedAt: str
    source: SourceInfo
    strategy: SplitStrategy = "smart"
    elements: list[Element] = []
    layers: list[Layer] = []               # bottom -> top
    canvas: Canvas = Canvas()
    lighting: Lighting = Lighting()
    camera: CameraSpec = CameraSpec()
    appearance: AppearanceId = "light"     # appearance currently being edited/previewed in the UI
    appearances: Appearances = Appearances()
    render: RenderSettings = RenderSettings()


# ----------------------------------------------------------------------------------------------
# Geometry bundle (server -> web & Blender). Produced by bis.svg.build_geometry().
# ----------------------------------------------------------------------------------------------
class SplinePoint(_Model):
    co: Vec2
    hl: Vec2      # left handle (absolute, art space)
    hr: Vec2      # right handle


class Spline(_Model):
    closed: bool = True
    hole: bool = False
    parent: int = -1          # index of the containing outer contour (holes), -1 for outers
    depth: int = 0            # nesting depth (0 = outer)
    points: list[SplinePoint]


class Region(_Model):
    elementId: str
    paint: Paint
    opacity: float = 1.0
    zSub: float = 0.0          # tiny z offset for translucent overlaps within a layer
    splines: list[Spline]


class LayerGeometry(_Model):
    layerId: str
    hash: str
    silhouette: list[Spline]   # union of all members (art space, before layer transform)
    regions: list[Region]      # occlusion-cut, disjoint pieces in paint order
    safeRadius: float          # largest bevel radius that will not invert any thin feature
    maxRadius: float = 0.0     # max inscribed-circle radius over the layer's bodies (art units): dome height at
                               # inflate 1 = inflate x maxRadius; body height H = thickness + 2 x inflate x maxRadius
    bbox: BBox                 # art space
    texture: str               # /files/... URL of the rasterised layer art (RGBA, edge-padded).
                               # Covers the art square −1..1: u=(x+1)/2, v=(y+1)/2.
    texturePath: str           # absolute filesystem path of the same PNG (for Blender)
    svg: str                   # /files/... URL of the standalone layer SVG (same viewBox)
    images: list[dict] = []    # raster cards: {elementId, path, url, bbox (art), opacity}


class GeometryBundle(_Model):
    projectId: str
    hash: str
    viewBox: tuple[float, float, float, float]
    plate: Optional[dict] = None          # {"bbox": art bbox of the detected source plate, "shape": "..."}
    layers: dict[str, LayerGeometry]      # keyed by layer id


# ----------------------------------------------------------------------------------------------
# Jobs (render / animate / export)
# ----------------------------------------------------------------------------------------------
class RenderRequest(_Model):
    quality: Quality = "draft"
    appearance: Optional[AppearanceId] = None     # default: project.appearance
    size: Optional[int] = None
    camera: Optional[CameraSpec] = None
    fullBleed: bool = False                        # square full-bleed plate, ortho_scale = 2 (App Store master)
    live: bool = False                             # coalesce with other live jobs for this project


class AnimateRequest(_Model):
    kind: Literal["turntable", "tilt", "float", "light-sweep", "iso", "explode"] = "tilt"   # explode = legacy alias of iso
    frames: int = 48
    fps: int = 24
    quality: Quality = "draft"
    size: int = 512
    format: Literal["mp4", "webp", "gif", "png"] = "mp4"


class ExportRequest(_Model):
    targets: list[Literal["ios", "macos", "watchos", "android", "windows", "web", "marketing", "icon", "blend"]] = ["ios"]
    appearances: list[AppearanceId] = ["light", "dark", "tinted-dark"]
    quality: Quality = "final"


# ----------------------------------------------------------------------------------------------
# Styles, looks and batch ("Icon Pack") — apply one look to many icons
# ----------------------------------------------------------------------------------------------
class StyleLayerDefaults(_Model):
    material: MaterialSpec = MaterialSpec()
    depth: LayerDepth = LayerDepth()
    shadow: LayerShadow = LayerShadow()
    mode: Optional[Literal["individual", "combined"]] = None   # None = keep each layer's own (tiling-derived) mode


class StylePlate(_Model):
    material: MaterialSpec = MaterialSpec(preset="satin")
    thickness: float = 0.16
    bevel: float = 0.04
    fill: Optional[Fill] = None          # None = keep each icon's own plate fill
    shape: Optional[PlateShape] = None   # None = keep each icon's detected shape


class StyleSpec(_Model):
    """A transferable look. Applying it to a project (see bis.style.apply_style):
    - every layer gets layerDefaults (material/depth/shadow/mode) — or layerMaterials[i] by index from the bottom
      (clamped to the last entry) when given; bevel is clamped to thickness/2;
    - layers are restacked with z_i = i * zGap (zGap None = keep z);
    - plate material/thickness/bevel (+ fill/shape when not None), lighting, camera (when given),
      render.colorMode (when given) and appearances.tint (when given) are copied."""
    layerDefaults: StyleLayerDefaults = StyleLayerDefaults()
    layerMaterials: Optional[list[MaterialSpec]] = None
    zGap: Optional[float] = 0.13
    plate: StylePlate = StylePlate()
    lighting: Optional[Lighting] = None
    camera: Optional[CameraSpec] = None
    colorMode: Optional[Literal["brand", "neutral", "standard", "agx", "agx-punchy"]] = None
    tint: Optional[Tint] = None


class StyleRequest(_Model):
    """POST /api/projects/{id}/style — exactly one of look / style / fromProject."""
    look: Optional[str] = None            # key into presets.json "looks"
    style: Optional[StyleSpec] = None
    fromProject: Optional[str] = None     # copy the style of another project (bis.style.extract_style)


class BatchSource(_Model):
    sample: Optional[str] = None          # sample name as listed by /api/samples
    projectId: Optional[str] = None       # existing project


class BatchRequest(_Model):
    """POST /api/batch — 'Icon Pack': create/update projects from many sources, apply one style, render
    each, optionally export everything into one zip."""
    sources: list[BatchSource]
    look: Optional[str] = None
    style: Optional[StyleSpec] = None
    fromProject: Optional[str] = None
    strategy: SplitStrategy = "smart"
    quality: Quality = "draft"            # per-icon render quality (tests: draft)
    size: int = 512
    appearance: AppearanceId = "light"
    export: Optional[ExportRequest] = None


JobKind = Literal["render", "animate", "export", "blend", "swatches", "batch"]
JobState = Literal["queued", "running", "done", "error", "cancelled"]


class Job(_Model):
    id: str
    projectId: Optional[str] = None
    kind: JobKind
    state: JobState = "queued"
    progress: float = 0.0
    message: str = ""
    createdAt: str
    startedAt: Optional[str] = None
    finishedAt: Optional[str] = None
    request: dict[str, Any] = {}
    result: Optional[dict[str, Any]] = None   # render: {url, path, width, height, seconds, engine, device}
    error: Optional[str] = None

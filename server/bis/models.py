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
    preset: str = "liquid_glass"              # key into shared/presets.json "materials"
    params: dict[str, Any] = {}               # overrides of preset param defaults


class LayerTransform(_Model):
    x: float = 0.0       # art units, applied after canvas.art
    y: float = 0.0
    scale: float = 1.0


class LayerDepth(_Model):
    z: float = 0.0              # back face of the layer above the plate's front face (art units)
    thickness: float = 0.10     # total extrusion thickness
    bevel: float = 0.045        # requested round-bevel radius (clamped to safeRadius by the builder)
    bevelSegments: int = 6
    inflate: float = 0.0        # 0..1 dome on the front face (reflections sweep across flat faces)


class LayerShadow(_Model):
    kind: Literal["none", "neutral", "chromatic"] = "neutral"
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
    explode: float = 1.0                 # multiplies layer z gaps (exploded hero shots)


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
    colorMode: Literal["neutral", "standard", "agx", "agx-punchy"] = "neutral"
    backdrop: Literal["transparent", "color", "wallpaper"] = "transparent"
    backdropColor: ColorHex = "#1c1c22"
    autoPreview: bool = True             # UI: auto Cycles preview after edits settle


class SourceInfo(_Model):
    filename: str
    viewBox: tuple[float, float, float, float]
    warnings: list[str] = []
    plateDetected: bool = False


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
    kind: Literal["turntable", "tilt", "float", "light-sweep", "explode"] = "tilt"
    frames: int = 48
    fps: int = 24
    quality: Quality = "draft"
    size: int = 512
    format: Literal["mp4", "webp", "gif", "png"] = "mp4"


class ExportRequest(_Model):
    targets: list[Literal["ios", "macos", "watchos", "android", "windows", "web", "marketing", "icon", "blend"]] = ["ios"]
    appearances: list[AppearanceId] = ["light", "dark", "tinted-dark"]
    quality: Quality = "final"


JobKind = Literal["render", "animate", "export", "blend", "swatches"]
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

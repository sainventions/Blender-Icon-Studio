// Shared data contract — hand-mirrored from server/bis/models.py. Keep both in sync.
// Coordinate spaces: SVG (viewBox, y down) · ART (centre origin, y UP, longer side = 2.0, i.e. −1..1)
// Canvas: plate spans −1..1; artwork root = canvas.art; layer transform after that; z toward camera.

export type Vec2 = [number, number]
export type BBox = [number, number, number, number] // minx, miny, maxx, maxy
export type ColorHex = string // '#rrggbb'

// ---------------------------------------------------------------- paint / fills
export interface GradientStop { offset: number; color: ColorHex; opacity: number }
export interface FillAuto { type: 'auto' }
export interface FillNone { type: 'none' }
export interface FillSolid { type: 'solid'; color: ColorHex; opacity: number }
export interface FillLinear { type: 'linear'; stops: GradientStop[]; start: Vec2; end: Vec2 }
export interface FillRadial {
  type: 'radial'; stops: GradientStop[]; center: Vec2; radius: number
  focal?: Vec2 | null; matrix?: [number, number, number, number, number, number] | null
}
export interface FillSystem { type: 'system-light' | 'system-dark' }
export type Fill = FillAuto | FillNone | FillSolid | FillLinear | FillRadial | FillSystem
export type Paint = FillNone | FillSolid | FillLinear | FillRadial

// ---------------------------------------------------------------- elements
export interface DropShadow { dx: number; dy: number; blur: number; opacity: number; color: ColorHex }
export interface SvgElement {
  id: string
  name: string
  kind: 'path' | 'image'
  paint: Paint
  opacity: number
  bbox: BBox // art space
  area: number // fraction of the art square
  groupPath: string[]
  origId?: string | null
  shadow?: DropShadow | null
  wasStroke: boolean
  role: 'fill' | 'stroke' | 'image'
}

// ---------------------------------------------------------------- layers
export interface MaterialSpec { preset: string; params: Record<string, number | string | boolean> }
export interface LayerTransform { x: number; y: number; scale: number }
export interface LayerDepth { z: number; thickness: number; bevel: number; bevelSegments: number; inflate: number }
export interface LayerShadow { kind: 'none' | 'neutral' | 'chromatic'; opacity: number }
export type BlendMode =
  | 'normal' | 'multiply' | 'screen' | 'overlay' | 'darken' | 'lighten'
  | 'soft-light' | 'hard-light' | 'plus-darker' | 'plus-lighter'

export interface Layer {
  id: string
  name: string
  elementIds: string[] // bottom -> top
  visible: boolean
  locked: boolean
  mode: 'individual' | 'combined'
  fill: Fill
  opacity: number
  blendMode: BlendMode
  glass: boolean
  transform: LayerTransform
  depth: LayerDepth
  material: MaterialSpec
  shadow: LayerShadow
}

// ---------------------------------------------------------------- document
export type Platform = 'ios' | 'macos' | 'watchos' | 'android' | 'windows' | 'web' | 'free'
export type PlateShape = 'squircle' | 'circle' | 'rounded' | 'square' | 'none'
export interface Plate { visible: boolean; fill: Fill; material: MaterialSpec; thickness: number; bevel: number }
export interface ArtTransform { scale: number; x: number; y: number }
export interface Canvas { platform: Platform; shape: PlateShape; cornerRadius: number; plate: Plate; art: ArtTransform }
export interface Lighting {
  preset: string; angle: number; elevation: number; intensity: number
  rim: number; fill: number; environment: number; shadowSoftness: number
}
export interface CameraSpec {
  view: 'front' | 'perspective'; tiltX: number; tiltY: number; fov: number; zoom: number; explode: number
}
export interface LayerOverride {
  fill?: Fill | null; opacity?: number | null; visible?: boolean | null
  blendMode?: BlendMode | null; material?: MaterialSpec | null
}
export interface AppearanceOverride { plateFill?: Fill | null; layers: Record<string, LayerOverride> }
export interface Tint { color: ColorHex; strength: number }
export interface Appearances { dark: AppearanceOverride; mono: AppearanceOverride; tint: Tint }
export type AppearanceId = 'light' | 'dark' | 'clear-light' | 'clear-dark' | 'tinted-light' | 'tinted-dark'
export type Quality = 'draft' | 'preview' | 'final' | 'ultra'
export interface RenderSettings {
  quality: Quality; size?: number | null
  colorMode: 'neutral' | 'standard' | 'agx' | 'agx-punchy'
  backdrop: 'transparent' | 'color' | 'wallpaper'; backdropColor: ColorHex
  autoPreview: boolean
}
export interface SourceInfo { filename: string; viewBox: [number, number, number, number]; warnings: string[]; plateDetected: boolean; fullBleed?: boolean }
export type SplitStrategy = 'smart' | 'group' | 'color' | 'element' | 'single'

export interface Project {
  version: number
  id: string
  name: string
  createdAt: string
  updatedAt: string
  source: SourceInfo
  strategy: SplitStrategy
  elements: SvgElement[]
  layers: Layer[] // bottom -> top
  canvas: Canvas
  lighting: Lighting
  camera: CameraSpec
  appearance: AppearanceId
  appearances: Appearances
  render: RenderSettings
}

// ---------------------------------------------------------------- geometry bundle
export interface SplinePoint { co: Vec2; hl: Vec2; hr: Vec2 }
export interface Spline { closed: boolean; hole: boolean; parent: number; depth: number; points: SplinePoint[] }
export interface Region { elementId: string; paint: Paint; opacity: number; zSub: number; splines: Spline[] }
export interface RasterCard {
  elementId: string; path: string; url: string; bbox: BBox; opacity: number
  matrix?: [number, number, number, number, number, number] // image pixel -> art space
  width?: number; height?: number; opaque?: boolean
}
export interface LayerGeometry {
  layerId: string
  hash: string
  silhouette: Spline[]
  regions: Region[]
  safeRadius: number
  bbox: BBox
  texture: string // URL; covers art square −1..1: u=(x+1)/2, v=(y+1)/2
  texturePath: string
  svg: string // URL
  images: RasterCard[]
}
export interface GeometryBundle {
  projectId: string
  hash: string
  viewBox: [number, number, number, number]
  plate?: { bbox: BBox; shape: string } | null
  layers: Record<string, LayerGeometry>
}

// ---------------------------------------------------------------- jobs
export interface RenderRequest {
  quality: Quality; appearance?: AppearanceId; size?: number; camera?: CameraSpec
  fullBleed?: boolean; live?: boolean
}
export interface AnimateRequest {
  kind: 'turntable' | 'tilt' | 'float' | 'light-sweep' | 'explode'
  frames: number; fps: number; quality: Quality; size: number; format: 'mp4' | 'webp' | 'gif' | 'png'
}
export type ExportTarget = 'ios' | 'macos' | 'watchos' | 'android' | 'windows' | 'web' | 'marketing' | 'icon' | 'blend'
export interface ExportRequest { targets: ExportTarget[]; appearances: AppearanceId[]; quality: Quality }
export type JobKind = 'render' | 'animate' | 'export' | 'blend' | 'swatches' | 'batch'
export type JobState = 'queued' | 'running' | 'done' | 'error' | 'cancelled'
export interface Job {
  id: string
  projectId?: string | null
  kind: JobKind
  state: JobState
  progress: number
  message: string
  createdAt: string
  startedAt?: string | null
  finishedAt?: string | null
  request: Record<string, unknown>
  result?: {
    url?: string; path?: string; width?: number; height?: number; seconds?: number
    engine?: string; device?: string; files?: { name: string; url: string }[]; zip?: string
    [k: string]: unknown
  } | null
  error?: string | null
}

// ---------------------------------------------------------------- presets (shared/presets.json via GET /api/presets)
export interface ParamSchema {
  label: string; type: 'number' | 'enum' | 'bool' | 'color'
  min?: number; max?: number; step?: number; default: number | string | boolean
  options?: string[]; unit?: string; help?: string
}
export interface MaterialPreset {
  label: string; category: string; description: string
  paint: 'tint' | 'base' | 'emission'
  engines: { cycles: string; eevee: string }
  eeveeNote?: string
  params: Record<string, ParamSchema>
  swatch?: string // URL of the pre-rendered swatch (added by the server if present)
}
export interface LightingPreset {
  label: string; description: string; key: number; rim: number; fill: number
  environment: number; warmth: number; lockAngle?: number; rimColors?: string[]
}
export interface PlatformSpec { label: string; shape: PlateShape; canvas: number; plateSize: number; appearances: AppearanceId[]; note?: string }
export interface QualitySpec { label: string; engine: 'eevee' | 'cycles'; size: number; samples: number; adaptiveThreshold?: number; description: string }
export interface Presets {
  version: number
  materials: Record<string, MaterialPreset>
  lighting: Record<string, LightingPreset>
  platforms: Record<string, PlatformSpec>
  appearances: Record<AppearanceId, { label: string; short: string }>
  quality: Record<Quality, QualitySpec>
  colorModes: Record<string, { label: string; viewTransform: string; look: string }>
  looks: Record<string, Look>
}

// ---------------------------------------------------------------- styles / looks / batch ("Icon Pack")
export interface StyleLayerDefaults { material: MaterialSpec; depth: LayerDepth; shadow: LayerShadow; mode: 'individual' | 'combined' }
export interface StylePlate { material: MaterialSpec; thickness: number; bevel: number; fill?: Fill | null; shape?: PlateShape | null }
export interface StyleSpec {
  layerDefaults: StyleLayerDefaults
  layerMaterials?: MaterialSpec[] | null
  zGap?: number | null
  plate: StylePlate
  lighting?: Lighting | null
  camera?: CameraSpec | null
  colorMode?: RenderSettings['colorMode'] | null
  tint?: Tint | null
}
export interface Look { label: string; description: string; style: Partial<StyleSpec> }
export interface StyleRequest { look?: string; style?: StyleSpec; fromProject?: string }
export interface BatchSource { sample?: string; projectId?: string }
export interface BatchRequest {
  sources: BatchSource[]
  look?: string; style?: StyleSpec; fromProject?: string
  strategy?: SplitStrategy; quality?: Quality; size?: number; appearance?: AppearanceId
  export?: ExportRequest | null
}
export interface BatchItemResult {
  source: BatchSource; projectId?: string; name: string; renderUrl?: string; error?: string
  exportUrl?: string // this icon's own export zip (batch with `export`)
  seconds?: number
}

// ---------------------------------------------------------------- system
export interface SystemStatus {
  blender: { found: boolean; path?: string; version?: string }
  worker: { state: 'stopped' | 'starting' | 'ready' | 'busy' | 'error'; pid?: number | null; message?: string }
  gpu: {
    name?: string; device?: string // e.g. 'OPTIX'
    memoryUsedMB?: number; memoryTotalMB?: number; utilization?: number; temperature?: number
  }
  queue: { queued: number; running: number }
}
export interface SampleIcon { name: string; file: string; url: string; thumbnail?: string; collection?: string }
export interface ProjectSummary { id: string; name: string; updatedAt: string; thumbnail?: string | null; layerCount: number }

// ---------------------------------------------------------------- websocket events (/ws)
export type WsEvent =
  | { type: 'job'; job: Job }
  | { type: 'system'; status: SystemStatus }
  | { type: 'project'; projectId: string; event: 'saved' | 'deleted' }

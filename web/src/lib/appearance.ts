// Appearance renditions (PLAN §5), mirroring the Blender worker's appearance.py rule for rule so the live viewport
// and the Blender renders of a rendition agree.
//
// resolveAppearance(project, id) returns an *effective* Project for rendering: overrides applied, materials of the
// clear / tinted renditions rewritten, lighting scaled. Colour transforms that are per-pixel (mono luminance, tint,
// tinted-dark glow) cannot be expressed in the Project schema, so they ride along as reserved, double-underscore
// material params ("intent flags") that lib/materials3d.ts reads. The effective project is a render-time value:
// never persist it, never show the `__*` params in the inspector.
import type { AppearanceId, AppearanceOverride, Fill, Layer, LayerOverride, MaterialSpec, Project } from '../types'

export type ColorIntent = 'color' | 'mono' | 'tint'

/** Reserved MaterialSpec.params keys written by resolveAppearance (never shown in the inspector, never saved). */
export const INTENT_KEYS = {
  intent: '__intent',
  tintColor: '__tintColor',
  tintStrength: '__tintStrength',
  emissive: '__emissive',
} as const

export interface MaterialIntent {
  intent: ColorIntent
  tintColor: string
  tintStrength: number
  /** Extra self-illumination of the paint colour (0..1), e.g. tinted-dark glyphs. */
  emissive: number
}

const DEFAULT_TINT = { color: '#3b82f6', strength: 0.8 }

export function readMaterialIntent(spec: MaterialSpec | null | undefined): MaterialIntent {
  const p = spec?.params ?? {}
  const raw = p[INTENT_KEYS.intent]
  const intent: ColorIntent = raw === 'mono' || raw === 'tint' ? raw : 'color'
  const tintColor =
    typeof p[INTENT_KEYS.tintColor] === 'string' ? (p[INTENT_KEYS.tintColor] as string) : DEFAULT_TINT.color
  const tintStrength =
    typeof p[INTENT_KEYS.tintStrength] === 'number' ? (p[INTENT_KEYS.tintStrength] as number) : DEFAULT_TINT.strength
  const emissive = typeof p[INTENT_KEYS.emissive] === 'number' ? (p[INTENT_KEYS.emissive] as number) : 0
  return { intent, tintColor, tintStrength, emissive }
}

export function isIntentParam(key: string): boolean {
  return key.startsWith('__')
}

/** Dark renditions (dimmer environment, dark plate, dark wallpaper). */
export function isDarkAppearance(a: AppearanceId): boolean {
  return a === 'dark' || a === 'clear-dark' || a === 'tinted-dark'
}

export function isClearAppearance(a: AppearanceId): boolean {
  return a === 'clear-light' || a === 'clear-dark'
}

/** PLAN §5: dark renditions use environment × 0.6 and key × 0.85. */
export const DARK_ENV_SCALE = 0.6
export const DARK_KEY_SCALE = 0.85

/**
 * Wallpapers behind the clear / tinted renditions — the same data as the worker's appearance.WALLPAPERS: a vertical
 * gradient (top → bottom over world y = +2.2 … −2.2) plus soft colour blobs centred at (x, y) × 1.6 (world units)
 * with a smoothstep falloff out to r × 1.9, mixed 0.85 in linear light. Colours are sRGB hex.
 */
export interface Wallpaper {
  top: string
  bottom: string
  blobs: { x: number; y: number; r: number; color: string }[]
}

export const WALLPAPERS: Record<'light' | 'dark', Wallpaper> = {
  light: {
    top: '#dfe9ff',
    bottom: '#f7e8f2',
    blobs: [
      { x: -0.9, y: 0.7, r: 0.9, color: '#9ec5ff' },
      { x: 0.95, y: -0.55, r: 0.85, color: '#ffc7dc' },
      { x: 0.2, y: 1.1, r: 0.6, color: '#c8f1e6' },
    ],
  },
  dark: {
    top: '#0b1630',
    bottom: '#05060c',
    blobs: [
      { x: -0.85, y: 0.6, r: 0.9, color: '#1d3f8f' },
      { x: 0.9, y: -0.6, r: 0.85, color: '#3b1d6e' },
      { x: 0.25, y: 1.15, r: 0.6, color: '#0f4a5c' },
    ],
  },
}

/** The wallpaper the worker places behind the icon for a rendition (null = none). */
export function appearanceWallpaper(a: AppearanceId): 'light' | 'dark' | null {
  if (a === 'clear-light' || a === 'tinted-light') return 'light'
  if (a === 'clear-dark' || a === 'tinted-dark') return 'dark'
  return null
}

const SYSTEM_DARK: Fill = { type: 'system-dark' }
/** Plate presets the tinted-dark rendition keeps (anything else becomes satin), as in the worker. */
const SOLID_PLATE_PRESETS = new Set(['satin', 'glossy_plastic', 'matte_clay'])

function mixHex(a: string, b: string, t: number): string {
  const pa = parseHex(a)
  const pb = parseHex(b)
  const c = pa.map((v, i) => Math.round(v + (pb[i] - v) * t))
  return '#' + c.map((v) => Math.max(0, Math.min(255, v)).toString(16).padStart(2, '0')).join('')
}

function parseHex(hex: string): [number, number, number] {
  let h = String(hex ?? '').trim().replace('#', '')
  if (h.length === 3)
    h = h
      .split('')
      .map((c) => c + c)
      .join('')
  const n = parseInt(h.slice(0, 6), 16)
  if (Number.isNaN(n)) return [255, 255, 255]
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
}

function applyLayerOverride(layer: Layer, ov: LayerOverride | null | undefined): Layer {
  if (!ov) return layer
  const out: Layer = { ...layer }
  if (ov.fill != null) out.fill = ov.fill
  if (ov.opacity != null) out.opacity = ov.opacity
  if (ov.visible != null) out.visible = ov.visible
  if (ov.blendMode != null) out.blendMode = ov.blendMode
  if (ov.material != null) out.material = ov.material
  return out
}

function applyOverride(p: Project, ov: AppearanceOverride | null | undefined): void {
  if (!ov) return
  if (ov.plateFill != null) p.canvas.plate.fill = ov.plateFill
  const map = ov.layers ?? {}
  p.layers = p.layers.map((l) => applyLayerOverride(l, map[l.id]))
}

function cloneForRender(project: Project, appearance: AppearanceId): Project {
  return {
    ...project,
    appearance,
    canvas: { ...project.canvas, plate: { ...project.canvas.plate }, art: { ...project.canvas.art } },
    lighting: { ...project.lighting },
    render: { ...project.render },
    camera: { ...project.camera },
    layers: project.layers.map((l) => ({ ...l })),
  }
}

/** Rewrite every *visible* layer as Liquid Glass with `params` (worker: hidden layers keep their material). */
function glassLayers(p: Project, params: Record<string, number | string | boolean>, fillNoneToAuto = false): void {
  p.layers = p.layers.map((l) =>
    l.visible
      ? {
          ...l,
          glass: true,
          fill: fillNoneToAuto && l.fill.type === 'none' ? { type: 'auto' } : l.fill,
          material: { preset: 'liquid_glass', params: { ...params } },
        }
      : l,
  )
}

/**
 * Effective project for one of the six renditions. `light` returns the input unchanged (same reference),
 * so memoised consumers do not rebuild anything. watchOS ignores appearances (always light).
 */
export function resolveAppearance(project: Project, appearance: AppearanceId): Project {
  const id: AppearanceId = project.canvas.platform === 'watchos' ? 'light' : appearance
  if (id === 'light') return project

  const p = cloneForRender(project, id)
  const aps = project.appearances
  const darkMode = isDarkAppearance(id)
  if (darkMode) {
    p.lighting.intensity *= DARK_KEY_SCALE
    p.lighting.environment *= DARK_ENV_SCALE
  }

  if (id === 'dark') {
    // The dark plate comes from appearances.dark.plateFill (its default is system-dark); null = keep the base fill.
    applyOverride(p, aps?.dark)
    return p
  }

  applyOverride(p, aps?.mono)
  const tint = { color: aps?.tint?.color || DEFAULT_TINT.color, strength: aps?.tint?.strength ?? DEFAULT_TINT.strength }
  const plate = p.canvas.plate

  if (id === 'clear-light' || id === 'clear-dark') {
    // Clear glass glyphs: frost follows the mono luminance (brightest art → white), plate = clear frosted pane.
    glassLayers(p, { tint: 0, frost: 0.3, translucency: 0.35, rim: 1, specular: 'auto', [INTENT_KEYS.intent]: 'mono' }, true)
    plate.material = { preset: 'frosted_glass', params: { tint: 0, frost: 0.42, grain: 0.04 } }
    plate.fill = { type: 'solid', color: '#ffffff', opacity: 1 }
    return p
  }

  const tintFlags = {
    [INTENT_KEYS.intent]: 'tint',
    [INTENT_KEYS.tintColor]: tint.color,
    [INTENT_KEYS.tintStrength]: tint.strength,
  }

  if (id === 'tinted-light') {
    // Mono luminance × tint colour into the glass base colour; plate = light frosted glass, tinted.
    glassLayers(p, { tint: 0.55 + 0.4 * tint.strength, frost: 0.18, translucency: 0.55, ...tintFlags })
    plate.material = { preset: 'frosted_glass', params: { tint: 0.5, frost: 0.4, grain: 0.04 } }
    plate.fill = { type: 'solid', color: mixHex('#ffffff', tint.color, 0.18 + 0.2 * tint.strength), opacity: 1 }
    return p
  }

  // tinted-dark: system-dark plate; foreground = mono luminance × tint, bright and slightly emissive.
  glassLayers(p, { tint: 0.85, frost: 0.12, translucency: 0.5, rim: 1, ...tintFlags, [INTENT_KEYS.emissive]: 0.35 })
  plate.fill = SYSTEM_DARK
  if (!SOLID_PLATE_PRESETS.has(plate.material.preset)) plate.material = { preset: 'satin', params: {} }
  return p
}

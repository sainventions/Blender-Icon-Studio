// Appearance renditions (PLAN §5, §11), mirroring blender_worker/appearance.py rule for rule: appearances only change
// Principled inputs (and the plate fill / backdrop), never the nature of the shader.
//
//   light        base project
//   dark         appearances.dark overrides (default plate fill system-dark); environment × 0.6, key × 0.85
//   clear-*      appearances.mono overrides; every visible layer → clear white glass (tint 0, transmission 1,
//                roughness 0.22); the plate → frosted glass over the wallpaper
//   tinted-light mono luminance × tint colour into the glass Base Color; plate = pale tinted frosted glass
//   tinted-dark  plate system-dark satin; glyphs = mono × tint glass with a little emission
//
// resolveAppearance returns the effective Project (render-time value: never persist it); the per-pixel mono transform
// of the tinted renditions is returned by appearanceMono (the worker's env `mono`), applied by lib/materials3d.
import type { AppearanceId, AppearanceOverride, Fill, Layer, LayerGeometry, LayerOverride, Paint, Project } from '../types'
import type { MonoParams } from './materials3d'

/** worker appearance.CLEAR_GLYPH / CLEAR_PLATE / TINTED_GLYPH / TINTED_PLATE / TINTED_DARK_GLOW. */
export const CLEAR_GLYPH = {
  tint: 0.0,
  transmission: 1.0,
  roughness: 0.22,
  ior: 1.5,
  metallic: 0.0,
  coatWeight: 0.6,
  coatRoughness: 0.03,
  emissionStrength: 0.0,
  paintMode: 'base',
} as const
export const CLEAR_PLATE = { tint: 0.0, transmission: 1.0, roughness: 0.45, ior: 1.45, coatWeight: 0.3 } as const
export const TINTED_GLYPH = {
  tint: 1.0,
  transmission: 1.0,
  roughness: 0.15,
  ior: 1.5,
  metallic: 0.0,
  coatWeight: 0.6,
  coatRoughness: 0.03,
  paintMode: 'base',
  emissionStrength: 0.0,
} as const
export const TINTED_DARK_GLOW = 0.5
export const TINTED_PLATE = { tint: 0.6, transmission: 1.0, roughness: 0.45, ior: 1.45, coatWeight: 0.3 } as const
/** Tinted renditions: luminance stretched to MONO_FLOOR..1 over at least MONO_MIN_RANGE (worker constants). */
export const MONO_FLOOR = 0.08
export const MONO_MIN_RANGE = 0.5

const DEFAULT_TINT = { color: '#3b82f6', strength: 0.8 }

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

export const srgbToLinear = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)

export function parseHex(hex: string): [number, number, number] {
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

/** worker appearance._mix_hex: sRGB mix of two hex colours. */
export function mixHex(a: string, b: string, t: number): string {
  const pa = parseHex(a)
  const pb = parseHex(b)
  const c = pa.map((v, i) => Math.round(Math.max(0, Math.min(1, (v + (pb[i] - v) * t) / 255)) * 255))
  return '#' + c.map((v) => v.toString(16).padStart(2, '0')).join('')
}

const hexLinear = (hex: string): [number, number, number] =>
  parseHex(hex).map((v) => srgbToLinear(v / 255)) as [number, number, number]

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

/** Every visible layer → Liquid Glass with `params` (per-shape overrides dropped: one look per rendition). */
function glassLayers(p: Project, params: Record<string, number | string>): void {
  p.layers = p.layers.map((l) =>
    l.visible
      ? {
          ...l,
          glass: true,
          fill: l.fill.type === 'none' ? { type: 'auto' } : l.fill,
          material: { preset: 'liquid_glass', params: { ...params } },
          elementMaterials: {},
        }
      : l,
  )
}

function fillColors(fill: Fill | Paint | null | undefined): string[] {
  if (!fill) return []
  if (fill.type === 'solid') return [fill.color]
  if (fill.type === 'linear' || fill.type === 'radial') return fill.stops.map((s) => s.color)
  if (fill.type === 'system-light') return ['#ffffff', '#e4e5ea']
  if (fill.type === 'system-dark') return ['#3a3a3f', '#111114']
  return []
}

/** worker appearance.luminance_range: linear-light luminance range over all visible foreground paint. */
export function luminanceRange(layers: Layer[], geometry: Record<string, LayerGeometry> | null | undefined): [number, number] {
  const vals: number[] = []
  for (const L of layers) {
    if (!L.visible) continue
    const fill = L.fill ?? { type: 'auto' }
    const cols = fill.type === 'auto' ? (geometry?.[L.id]?.regions ?? []).flatMap((r) => fillColors(r.paint)) : fillColors(fill)
    for (const c of cols) {
      const [r, g, b] = hexLinear(c)
      vals.push(0.2126 * r + 0.7152 * g + 0.0722 * b)
    }
  }
  if (!vals.length) return [0, 1]
  return [Math.min(...vals), Math.max(...vals)]
}

/**
 * Effective project for one of the six renditions. `light` returns the input unchanged (same reference), so memoised
 * consumers do not rebuild anything. watchOS ignores appearances (always light).
 */
export function resolveAppearance(project: Project, appearance: AppearanceId): Project {
  const id: AppearanceId = project.canvas.platform === 'watchos' ? 'light' : appearance
  if (id === 'light') return project
  const p = cloneForRender(project, id)
  const aps = project.appearances
  if (isDarkAppearance(id)) {
    p.lighting.intensity *= DARK_KEY_SCALE
    p.lighting.environment *= DARK_ENV_SCALE
  }
  if (id === 'dark') {
    applyOverride(p, aps?.dark)
    return p
  }
  applyOverride(p, aps?.mono)
  const plate = p.canvas.plate
  if (isClearAppearance(id)) {
    glassLayers(p, CLEAR_GLYPH)
    plate.material = { preset: 'frosted_glass', params: { ...CLEAR_PLATE } }
    plate.fill = { type: 'solid', color: '#ffffff', opacity: 1 }
    return p
  }
  const tint = { color: aps?.tint?.color || DEFAULT_TINT.color, strength: aps?.tint?.strength ?? DEFAULT_TINT.strength }
  const strength = Math.max(0, Math.min(1, tint.strength))
  if (id === 'tinted-dark') {
    glassLayers(p, { ...TINTED_GLYPH, paintMode: 'base+emission', emissionStrength: TINTED_DARK_GLOW })
    plate.fill = SYSTEM_DARK
    if (!SOLID_PLATE_PRESETS.has(plate.material.preset)) plate.material = { preset: 'satin', params: {} }
  } else {
    glassLayers(p, TINTED_GLYPH)
    plate.material = { preset: 'frosted_glass', params: { ...TINTED_PLATE } }
    plate.fill = { type: 'solid', color: mixHex('#ffffff', tint.color, 0.18 + 0.2 * strength), opacity: 1 }
  }
  return p
}

/**
 * The worker's env `mono` of a rendition (tinted renditions only, else null): art luminance stretched over the icon's
 * luminance range (at least MONO_MIN_RANGE) to MONO_FLOOR..1, × the tint colour (linear, mixed from white by strength).
 */
export function appearanceMono(
  project: Project,
  appearance: AppearanceId,
  geometry: Record<string, LayerGeometry> | null | undefined,
): MonoParams | null {
  const id: AppearanceId = project.canvas.platform === 'watchos' ? 'light' : appearance
  if (id !== 'tinted-light' && id !== 'tinted-dark') return null
  const p = cloneForRender(project, id)
  applyOverride(p, project.appearances?.mono)
  const [lo, hi] = luminanceRange(p.layers, geometry)
  const t = project.appearances?.tint ?? DEFAULT_TINT
  const strength = Math.max(0, Math.min(1, Number(t.strength ?? DEFAULT_TINT.strength)))
  const tl = hexLinear(t.color || DEFAULT_TINT.color)
  return {
    lo: Math.min(lo, hi - MONO_MIN_RANGE),
    hi,
    floor: MONO_FLOOR,
    tint: [1 + (tl[0] - 1) * strength, 1 + (tl[1] - 1) * strength, 1 + (tl[2] - 1) * strength],
  }
}

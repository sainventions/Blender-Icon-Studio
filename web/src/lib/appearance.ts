// Appearance renditions (PLAN §5), mirroring the Blender worker's appearance.py rule for rule so the live viewport
// and the Blender renders of a rendition agree.
//
// resolveAppearance(project, id) returns an *effective* Project for rendering: overrides applied, materials of the
// clear / tinted renditions rewritten, lighting scaled. Colour transforms that are per-pixel (mono luminance, tint,
// tinted-dark glow) cannot be expressed in the Project schema, so they ride along as reserved, double-underscore
// material params ("intent flags") that lib/materials3d.ts reads. The effective project is a render-time value:
// never persist it, never show the `__*` params in the inspector.
import type {
  AppearanceId,
  AppearanceOverride,
  Fill,
  Layer,
  LayerGeometry,
  LayerOverride,
  MaterialSpec,
  Paint,
  Project,
} from '../types'

export type ColorIntent = 'color' | 'mono' | 'tint'

/** Reserved MaterialSpec.params keys written by resolveAppearance (never shown in the inspector, never saved). */
export const INTENT_KEYS = {
  intent: '__intent',
  tintColor: '__tintColor',
  tintStrength: '__tintStrength',
  emissive: '__emissive',
  /** Clear-light: share of the Liquid Glass transmission removed toward the outline (worker env `edgeDark`). */
  edgeDark: '__edgeDark',
} as const

export interface MaterialIntent {
  intent: ColorIntent
  tintColor: string
  tintStrength: number
  /** Extra self-illumination of the paint colour (0..1), e.g. tinted-dark glyphs. */
  emissive: number
  /** Liquid Glass edge darkening (0 = off; clear-light rendition). */
  edgeDark: number
}

/**
 * Clear renditions (worker appearance.CLEAR_*): glyph glass tint (mono luminance, tint 0.5 → 94 % of the grey), the
 * inner glow per mode, clear-light's rim darkening, drop-shadow floor and faintly smoked pale plate.
 */
export const CLEAR = {
  tint: 0.5,
  edgeDark: 0.8,
  lightShadow: 0.8,
  glowLight: 0.1,
  glowDark: 0.3,
  lightPlate: '#c9ccd6',
  lightPlateTint: 0.4,
} as const

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
  const edgeDark = typeof p[INTENT_KEYS.edgeDark] === 'number' ? (p[INTENT_KEYS.edgeDark] as number) : 0
  return { intent, tintColor, tintStrength, emissive, edgeDark }
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

/**
 * Mono / tint renditions (worker appearance.MONO_FLOOR / materials.MONO_MIN_RANGE): the perceptual luminance of the
 * art is stretched from the icon-wide range [lo, hi] onto MONO_FLOOR..1 (brightest → white); a narrow range is never
 * over-stretched (lo ≤ hi − MONO_MIN_RANGE). PLAN §10: the floor is 0.3 (supersedes 0.25 in §5).
 */
export const MONO_FLOOR = 0.3
export const MONO_MIN_RANGE = 0.55

/** Worker `_mono`: perceptual luminance (0..1) → stretched mono grey level (perceptual, MONO_FLOOR..1). */
export function monoLevel(perceptualLum: number, lo: number, hi: number): number {
  const l = Math.min(lo, hi - MONO_MIN_RANGE)
  const t = Math.max(0, Math.min(1, (perceptualLum - l) / Math.max(hi - l, 1e-4)))
  return MONO_FLOOR + (1 - MONO_FLOOR) * t
}

// ------------------------------------------------------------------------------------------------ round-5 mono maps
/**
 * Worker appearance.CLEAR_MONO_FLOOR / CLEAR_MONO_GAMMA (clear renditions: floor + (1 − floor)·s^γ of the linear
 * stretch s — mid paints lifted toward frosted white, dark details stay dark), CLEAR_COMBINED_FLOOR / _LINEAR (combined
 * bodies: rank-spread map, internal boundaries keep visible steps), CLEAR_DARK_COAT (clear-dark pane: weaker coat,
 * IOR 1.3), CLEAR_LIGHT_SMOKE (clear-light pane = CLEAR_LIGHT_PLATE × this in linear light), TINT_LIGHT_GAIN
 * (tinted-light: mono × tint at min(1, strength + gain)), TINT_DARK_FLOOR (tinted-dark map floor), MONO_LINEAR /
 * MONO_MERGE (rank-spread map: linear share, merge distance of paint lightnesses).
 */
export const CLEAR_MONO_FLOOR = 0.3
export const CLEAR_MONO_GAMMA = 0.35
export const CLEAR_DARK_COAT = 0.3
export const CLEAR_COMBINED_FLOOR = 0.4
export const CLEAR_COMBINED_LINEAR = 0.5
export const CLEAR_LIGHT_SMOKE = 0.32
export const TINT_LIGHT_GAIN = 0.35
export const TINT_DARK_FLOOR = 0.5
export const MONO_LINEAR = 0.3
export const MONO_MERGE = 0.015

/** Monotone map perceptual paint lightness → mono level as [(lightness, value)] colour-ramp stops (worker `lut`). */
export type MonoLut = [number, number][]

/** Python's round(): half to even (the worker thins long stop lists with int(round(i · step))). */
function pyRound(x: number): number {
  const f = Math.floor(x)
  const d = x - f
  if (Math.abs(d - 0.5) < 1e-12) return f % 2 === 0 ? f : f + 1
  return Math.round(x)
}

/** Sorted distinct lightnesses, merged within MONO_MERGE (worker mono_lut / gamma_lut). */
function distinctLightness(vals: number[]): number[] {
  const vs: number[] = []
  for (const v of [...vals].sort((a, b) => a - b)) if (!vs.length || v - vs[vs.length - 1] > MONO_MERGE) vs.push(v)
  return vs
}

/**
 * Worker appearance.mono_lut: MONO_LINEAR × the linear stretch of the range (lo ≤ hi − minRange) + the rest by rank of
 * the distinct lightnesses (similar paints stay apart), floor..1, the brightest paint → 1.
 */
export function monoLut(vals: number[], floor: number, minRange = MONO_MIN_RANGE, linear = MONO_LINEAR): MonoLut {
  const vs = distinctLightness(vals)
  if (!vs.length) return [[0, floor], [1, 1]]
  const hi = vs[vs.length - 1]
  const lo = Math.min(vs[0], hi - minRange)
  const n = vs.length
  let out: MonoLut = lo >= vs[0] - 1e-6 ? [] : [[Math.max(0, lo), floor]]
  vs.forEach((v, i) => {
    const lin = (v - lo) / Math.max(1e-6, hi - lo)
    const rank = n > 1 ? i / (n - 1) : 1
    out.push([v, floor + (1 - floor) * (linear * lin + (1 - linear) * rank)])
  })
  if (out.length > 30) {
    const step = (out.length - 1) / 29
    const src = out
    out = Array.from({ length: 30 }, (_, i) => src[pyRound(i * step)])
  }
  return out
}

/**
 * Worker appearance.gamma_lut: floor + (1 − floor)·s^gamma of the linear stretch s (lo ≤ hi − minRange), stops on every
 * distinct paint lightness plus along the curve.
 */
export function gammaLut(vals: number[], floor: number, gamma: number, minRange = MONO_MIN_RANGE): MonoLut {
  let vs = distinctLightness(vals)
  if (!vs.length) return [[0, floor], [1, 1]]
  const hi = vs[vs.length - 1]
  const lo = Math.max(0, Math.min(vs[0], hi - minRange))
  const span = Math.max(1e-6, hi - lo)
  const f = (v: number) => floor + (1 - floor) * Math.min(1, Math.max(0, (v - lo) / span)) ** gamma
  if (vs.length > 29) {
    const step = (vs.length - 1) / 28
    const src = vs
    vs = Array.from({ length: 29 }, (_, i) => src[pyRound(i * step)])
  }
  const pts = [...new Set([...vs, lo])]
  for (const s of [0.01, 0.03, 0.07, 0.13, 0.22, 0.35, 0.5, 0.7, 0.85]) {
    if (pts.length >= 30) break
    const c = lo + span * s
    if (pts.every((v) => Math.abs(c - v) > 0.004)) pts.push(c)
  }
  return pts.sort((a, b) => a - b).map((v) => [v, f(v)] as [number, number])
}

/** A colour ramp (Blender ValToRGB, linear): the LUT value at lightness `l` (clamped to the end stops). */
export function lutValue(lut: MonoLut, l: number): number {
  if (!lut.length) return 1
  if (l <= lut[0][0]) return lut[0][1]
  for (let i = 1; i < lut.length; i++) {
    const [x1, y1] = lut[i]
    if (l <= x1) {
      const [x0, y0] = lut[i - 1]
      return x1 - x0 > 1e-12 ? y0 + ((y1 - y0) * (l - x0)) / (x1 - x0) : y1
    }
  }
  return lut[lut.length - 1][1]
}

const srgbToLinear = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)
const linearToSrgb = (c: number) => (c <= 0.0031308 ? c * 12.92 : 1.055 * Math.max(0, c) ** (1 / 2.4) - 0.055)

/** Worker appearance._perceptual: (linear Rec.709 luminance)^(1/2.2) of an sRGB hex colour. */
export function perceptualLightness(hex: string): number {
  const [r, g, b] = parseHex(hex).map((v) => srgbToLinear(v / 255))
  return Math.max(0, 0.2126 * r + 0.7152 * g + 0.0722 * b) ** (1 / 2.2)
}

function fillColors(fill: Fill | Paint | null | undefined): string[] {
  if (!fill) return []
  if (fill.type === 'solid') return [fill.color || '#ffffff']
  if (fill.type === 'linear' || fill.type === 'radial') return (fill.stops ?? []).map((s) => s.color || '#000000')
  if (fill.type === 'system-light') return ['#ffffff', '#e4e5ea']
  if (fill.type === 'system-dark') return ['#3a3a3f', '#111114']
  return []
}

/** Worker appearance.paint_lightness: perceptual lightness of every visible foreground paint (fills or region paints). */
export function paintLightness(layers: Layer[], geometry: Record<string, LayerGeometry> | null | undefined): number[] {
  const vals: number[] = []
  for (const L of layers) {
    if (!L.visible) continue
    const fill = L.fill ?? { type: 'auto' }
    const cols =
      fill.type === 'auto' ? (geometry?.[L.id]?.regions ?? []).flatMap((r) => fillColors(r.paint)) : fillColors(fill)
    for (const c of cols) vals.push(perceptualLightness(c))
  }
  return vals
}

/** The mono maps of a rendition (worker env mono / monoCombined `lut`): null for light / dark. */
export function monoLutsFor(
  appearance: AppearanceId,
  layers: Layer[],
  geometry: Record<string, LayerGeometry> | null | undefined,
): { mono: MonoLut; combined: MonoLut } | null {
  if (appearance === 'light' || appearance === 'dark') return null
  const vals = paintLightness(layers, geometry)
  if (appearance === 'clear-light' || appearance === 'clear-dark') {
    return {
      mono: gammaLut(vals, CLEAR_MONO_FLOOR, CLEAR_MONO_GAMMA),
      combined: monoLut(vals, CLEAR_COMBINED_FLOOR, MONO_MIN_RANGE, CLEAR_COMBINED_LINEAR),
    }
  }
  const lut = monoLut(vals, appearance === 'tinted-dark' ? TINT_DARK_FLOOR : MONO_FLOOR)
  return { mono: lut, combined: lut }
}

/** Worker appearance._scale_hex: an sRGB hex colour scaled by `k` in linear light. */
export function scaleHex(hex: string, k: number): string {
  const c = parseHex(hex).map((v) => Math.round(Math.max(0, Math.min(1, linearToSrgb(srgbToLinear(v / 255) * k))) * 255))
  return '#' + c.map((v) => v.toString(16).padStart(2, '0')).join('')
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
    // Clear glass glyphs tinted by the mono luminance (dark parts stay dark smoked glass, brightest → white) whose
    // frost follows it too. clear-light: a faintly smoked pale plate, a darker lensed rim (edge darkening), a deeper
    // drop shadow and a low inner glow so the white glyph reads; clear-dark: a white frosted pane, moderate glow.
    const dark = id === 'clear-dark'
    glassLayers(
      p,
      {
        tint: CLEAR.tint,
        frost: 0.3,
        translucency: 0.35,
        rim: 1,
        specular: 'auto',
        glow: dark ? CLEAR.glowDark : CLEAR.glowLight,
        [INTENT_KEYS.intent]: 'mono',
        ...(dark ? {} : { [INTENT_KEYS.edgeDark]: CLEAR.edgeDark }),
      },
      true,
    )
    if (!dark) {
      p.layers = p.layers.map(
        (l): Layer =>
          l.visible
            ? { ...l, shadow: { kind: 'neutral', opacity: Math.max(CLEAR.lightShadow, Number(l.shadow?.opacity ?? 0.5) || 0) } }
            : l,
      )
      // a smoky pane (CLEAR_LIGHT_PLATE × CLEAR_LIGHT_SMOKE in linear light): white glyphs read against it
      plate.material = { preset: 'frosted_glass', params: { tint: CLEAR.lightPlateTint, frost: 0.42, grain: 0.04 } }
      plate.fill = { type: 'solid', color: scaleHex(CLEAR.lightPlate, CLEAR_LIGHT_SMOKE), opacity: 1 }
    } else {
      // a weaker coat / lower index: the pane's studio reflection read as a grey veil over the deep wallpaper
      plate.material = {
        preset: 'frosted_glass',
        params: { tint: 0, frost: 0.42, grain: 0.04, coat: CLEAR_DARK_COAT, ior: 1.3 },
      }
      plate.fill = { type: 'solid', color: '#ffffff', opacity: 1 }
    }
    return p
  }

  const tintFlags = {
    [INTENT_KEYS.intent]: 'tint',
    [INTENT_KEYS.tintColor]: tint.color,
    [INTENT_KEYS.tintStrength]: tint.strength,
  }

  if (id === 'tinted-light') {
    // Mono luminance × tint colour (at min(1, strength + TINT_LIGHT_GAIN): the glyph keeps its contrast) into the glass
    // base colour; plate = light frosted glass, tinted.
    glassLayers(p, {
      tint: 0.55 + 0.4 * tint.strength,
      frost: 0.18,
      translucency: 0.55,
      ...tintFlags,
      [INTENT_KEYS.tintStrength]: Math.min(1, tint.strength + TINT_LIGHT_GAIN),
    })
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

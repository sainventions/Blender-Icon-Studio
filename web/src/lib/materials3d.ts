// three.js approximations of every material preset in shared/presets.json (recipes: glass-materials-blender.md §3).
//
// describeMaterial()  preset + params (+ appearance intent flags) → IconMaterialSpec (pure data)
// IconMaterial        MeshPhysicalMaterial + a small shader patch that mirrors the Blender node graphs:
//                       · paint read from the layer texture in object XY (u=(x+1)/2, v=(y+1)/2; raster cards use
//                         their image placement), mixed per the preset's `paint` mode: tint (glass base colour =
//                         mix(white, paint, tintCurve(tint)) in gamma space, like the worker), base, emission;
//                         raster-image regions honour the texture alpha
//                       · mono / tint colour intents (appearances) as per-pixel luminance transforms
//                       · light-angle-locked rim emission + inner glow (Liquid Glass "specular" / "glow")
//                       · neon hot core, per-pixel Beer–Lambert attenuation colour, front-face "inflate" dome
//                       · "fake glass" for glass layers covered by other glass (PLAN D7): opaque, shows the plate
//                         (or backdrop) through itself so the top glass can still refract it
//                       · colour fidelity (worker round 4/5): paints are pre-compensated for the colour mode's view
//                         transform (displayPaint: inverse soft clip in 'brand', inverse Khronos PBR Neutral in
//                         'neutral'), diffuse presets get the albedo the calibrated rig lights to that radiance
//                         (diffuseAlbedo), Liquid Glass / flat emit it; translucent Liquid Glass pieces are blend films
// applyIconMaterial() pushes a spec + context into an IconMaterial in place (recompiles only on topology change).
import * as THREE from 'three'
import type { MaterialSpec, Presets } from '../types'
import { lutValue, MONO_FLOOR, MONO_MIN_RANGE, readMaterialIntent, type MaterialIntent, type MonoLut } from './appearance'
import { getFilmNoiseTexture, getGrainNormalMap, getRadialAnisotropyMap } from '../viewport/textures/procedural'
import { BRAND_CAP, BRAND_KNEE, colorModeId, softClip, softClipInverse } from '../viewport/scene/displayTransform'

export type PaintMode = 'tint' | 'base' | 'emission'
export type RGB = [number, number, number]
type ParamValue = number | string | boolean
type Params = Record<string, ParamValue>

export interface IconMaterialSpec {
  preset: string
  paintMode: PaintMode
  /** material.color (linear); a dark base for emissive presets. Paint multiplies it in tint/base modes. */
  baseColor: RGB
  /** Paint mix origin: mix(neutral, paint, paintMix). */
  neutral: RGB
  paintMix: number
  /**
   * Glass presets (worker `tinted()`): the mix happens in perceptual (gamma 2.2) space and paintMix is already
   * the eased `tintCurve(tint)`, so the default tint keeps brand colours vivid exactly like the Blender render.
   */
  paintPerceptual: boolean
  /**
   * Glass whose Cycles body is a Glass BSDF (worker b_dispersive_crystal): its colour, sqrt(body), tints the light at
   * BOTH interfaces, so over a white plate Cycles shows the body itself — the transmitted light keeps the body colour
   * once (three.js), not the Principled presets' GLASS_TRANSMIT · sqrt(body) calibration (round-5 review: Gmail's red
   * prism M went mauve, live (159, 115, 112) against Cycles (204, 83, 76)).
   */
  transmitBody: boolean
  roughness: number
  metalness: number
  /** > 0 → the preset wants real refraction (three.js transmission). */
  transmission: number
  /**
   * Icon Composer translucency (mirrors the worker): a milky, paint-coloured fraction that grows from the bottom
   * of the layer (×0.45) to its top (×1.0): transmission × (1 − clamp(fall(y) · milk)). 0 = clear everywhere.
   */
  milk: number
  /** Clear renditions: the milky fraction follows the mono luminance (bright art → white frost, dark → clear). */
  milkByLum: boolean
  ior: number
  /** MeshPhysicalMaterial.thickness = layer thickness × this (exaggerated so the pill edges visibly lens). */
  thicknessScale: number
  clearcoat: number
  clearcoatRoughness: number
  specularIntensity: number
  envMapIntensity: number
  sheen: number
  sheenRoughness: number
  /** 0 = white sheen, 1 = sheen in the paint colour. */
  sheenTint: number
  iridescence: number
  iridescenceIOR: number
  iridescenceRange: [number, number]
  iridescenceBands: number
  /** three.js `dispersion` (already converted from the preset's IOR spread). */
  dispersion: number
  anisotropy: number
  anisotropyRadial: boolean
  /** Sandblast micro-normal strength (frosted glass grain). */
  grain: number
  /** 1 = Beer–Lambert attenuation colour follows the paint. */
  attenuationMix: number
  /** attenuationDistance = material.thickness × this. */
  attenuationDistanceScale: number
  /** Self-illumination of the paint colour (emission presets: strength). */
  emissive: number
  /** Worker _glass_common(white_milk): white paints get at least this frosted (milky) share (clear glass). */
  whiteMilk: number
  /**
   * Worker _albedo(display_paint(col), B): diffuse presets take the albedo that the calibrated rig lights to the
   * displayed paint (radiance = DIFFUSE_A · albedo + B face-on). null = the paint is used as-is.
   */
  albedoB: number | null
  /**
   * Live-only diffuse response gain (1 = none): matte clay's Principled "Diffuse Roughness 0.5" (energy-preserving
   * Oren–Nayar) lights a face-on clay surface MATTE_CLAY_RESPONSE × a Lambert one in Cycles, for every rig component
   * (measured on a grey plate under all lighting presets); three.js' diffuse is Lambert.
   */
  albedoGain: number
  /** Worker b_flat: the emission is the radiance the view transform displays as the paint (display_paint). */
  displayEmission: boolean
  /**
   * Worker b_satin: the face-on specular takes the paint's hue (Specular Tint = rad / max(rad)) and the reflection
   * taken off the albedo (B) is tinted alike, so zero-channel brand colours keep their zero channel.
   */
  specularTint: boolean
  /** Neon hot core: mix toward white where the tube faces the camera. */
  core: number
  rim: { key: number; back: number; glow: number }
  /** Liquid Glass shading model (worker b_liquid_glass); null for every other preset. */
  lg: LiquidGlassModel | null
  /** Requested compositor bloom (neon). */
  bloom: number
  unlit: boolean
  intent: MaterialIntent
}

const WHITE: RGB = [1, 1, 1]

/**
 * Worker b_liquid_glass (art-directed Icon Composer look, round-4 colour fidelity), mirrored term by term in the shader
 * (BIS_LG). With w = whiteness(paint)^1.5, L = perceptual lightness of the paint (luminance^(1/2.2); dark paints →
 * smoked glass), rad = displayPaint(body) (the radiance the view transform shows as the paint):
 *  · face = mix(clear glass, self-lit fill, fill) with fill = mix(1 − tCap, 1 − 0.35 tCap, w) · (0.94 → 1.0 bottom →
 *    top) [· clamp(1.15 · mono lum) in clear renditions] · (LG_EDGE_BODY → 1 smoothstep over e 0 → 0.45), where
 *    e = 1 − |N.xy| (0 = silhouette, 1 = cap) and tCap = liquidGlassClearShare(transl) (16 % at the default 0.75);
 *  · self-lit fill = 0.15 · diffuse(albedo(rad − LG_COAT_B)) + 0.85 · emission((rad − LG_COAT_B) · (0.95 → 1.04
 *    bottom → top) · lit · face) under the coat, face = 1 + 0.45 · edgeDark;
 *  · clear glass tinted t2^(2γ), t2 = min(rad / ALBEDO_MAX, 1), γ = 0.9 → 0.5 per interface over e 0 → 0.6 (Cycles
 *    tints at both interfaces, three.js once: the exponent is doubled; translucent pieces: untinted, t2 = 1) [× (1 −
 *    edgeDark → 1, smoothstep over e 0 → 0.6) in clear-light]; fake glass (under other glass) shows albedo(rad);
 *    specular level 0.4 and coat
 *    both faded out toward the silhouette (e 0.08 → 0.6) and × dk = (0.35 → 1 over L 0 → 0.5) for dark paints;
 *  · rim = 5 · rim · (key² + 0.16 · back²) · band(e, RIM_BANDS[specular]) · (0.06 → 1, smoothstep over L 0.1 → 0.8),
 *    key/back = ±dot(N.xy, L.xy) normalised;
 *  · glow = mix(body, white, 0.3 L) · band(e, 0.3, 0.45, 0.8, 0.95) · (0.08 + 0.92 · back) · 1.4 · glow · lit;
 *  · lit = clamp(0.55 + 0.45 · key light, 0.4, 1.4) (MaterialContext.lit).
 */
export interface LiquidGlassModel {
  /** Fill share of the flat cap for saturated (x) / white (y) paint: 1 − tCap, 1 − 0.35 · tCap. */
  fill: [number, number]
  /** Rim emission strength (5 · rim; 0 when specular is off). */
  rim: number
  /** Rim band over e: smoothstep(a0, a1) · (1 − smoothstep(b0, b1)). */
  band: [number, number, number, number]
  /** Inner glow amount (preset `glow`). */
  glow: number
  /** Clear-light edge darkening of the clear glass toward the outline (0 = off; appearance intent `edgeDark`). */
  edgeDark: number
}

/**
 * Worker RIM_BANDS (specular placement of the Liquid Glass rim over e = 1 − |N.xy|). Round 5: the 'auto' band hugs the
 * outline (was 0.015-0.06-0.24-0.4) — a crisp thin highlight instead of a wide pale band on saturated glyphs.
 */
export const LG_RIM_BANDS: Record<string, [number, number, number, number]> = {
  auto: [0.008, 0.025, 0.09, 0.17],
  inside: [0.3, 0.42, 0.6, 0.78],
  outside: [0.0, 0.005, 0.06, 0.14],
}
/** Worker Liquid Glass: diffuse share of the self-lit fill (the rest is emission): mix_shader(0.85, diffuse, emission). */
export const LG_FILL_DIFFUSE = 0.15
/** Worker Liquid Glass clear tint: per-interface gamma of t2 at the silhouette / on the cap (e 0 → 0.6). */
export const LG_DEEP: [number, number] = [0.9, 0.5]

// ---------------------------------------------------------------- worker round-4 colour calibration (mirrored)
/** materials.py: face-on radiance of a diffuse surface under the calibrated rig = DIFFUSE_A · albedo + DIFFUSE_B. */
export const DIFFUSE_A = 1.0
export const DIFFUSE_B = 0.017
/** materials.py: brightest diffuse albedo used to reach a paint (hue kept when capped). */
export const ALBEDO_MAX = 1.3
/** materials.py: radiance the Liquid Glass coat reflects of the studio world (taken off the emitted fill). */
export const LG_COAT_B = 0.01
/** util.py: Khronos PBR Neutral highlight-compression start (0.8 − 0.04) and desaturation. */
export const PBR_START = 0.76
export const PBR_DESAT = 0.15
/** util.py: target-peak caps of display_paint for neutral (x) / fully saturated (y) paints, blended by saturation^POW. */
export const NEUTRAL_CAP: [number, number] = [0.975, 0.86]
export const NEUTRAL_CAP_POW = 12
/**
 * materials.py: brightest displayed peak a diffuse surface can reach — PBR Neutral's pre-image of that peak is
 * ALBEDO_MAX + DIFFUSE_B, so diffuse presets cap their display_paint target there (a brighter target would exceed
 * ALBEDO_MAX and the whole colour would be scaled darker).
 */
export const DIFFUSE_PEAK = 1 - (1 - PBR_START) ** 2 / (ALBEDO_MAX + DIFFUSE_B - (2 * PBR_START - 1))
/** materials.py: the B offset each diffuse preset passes to diffuse_paint (b_satin uses DIFFUSE_B). */
export const ALBEDO_B: Record<string, number> = {
  satin: DIFFUSE_B,
  glossy_plastic: 0.03,
  candy: 0.03,
  gummy: 0.02,
  matte_clay: 0.01,
}

/** materials.py LG_CAP_CLEAR: Liquid Glass clear (see-through) share of the flat cap at the default translucency. */
export const LG_CAP_CLEAR = 0.16
/** materials.py LG_EDGE_BODY: Liquid Glass body share left at the silhouette (the bevel lenses what lies beneath). */
export const LG_EDGE_BODY = 0.35
/** materials.py TRANSLUCENT_CLEAR: how untinted a translucent Liquid Glass piece's clear share is. */
export const TRANSLUCENT_CLEAR = 0.3
/** materials.py CLEAR_WHITE_MILK: clear glass — frosted share of a white paint's body (readable white glyphs). */
export const CLEAR_WHITE_MILK = 0.6
/**
 * materials.py WHITE_ICE: frosted glass — the same for white paints (prism uses CLEAR_WHITE_MILK; worker _white_ice):
 * a frosted-white body share whiteness^1.5 × amount × (0.8 → 1 bottom → top). Never on the plate or in clear renditions.
 */
export const WHITE_ICE = 0.6
/**
 * Round 5 Liquid Glass (worker b_liquid_glass): near-black paints (lightness 0.04 → 0.22: nb 0 → 1) are opaque smoked
 * glass — no rim, no clear pill edge, coat only on the cap (e 0.6 → 0.9: 0 → 0.5); thin strokes (bevel ≥ THIN_RATIO ×
 * safeRadius, scene.py) keep THIN_SOLID of their body across the bevel ('solid_edge').
 */
export const LG_NEAR_BLACK: [number, number] = [0.04, 0.22]
export const THIN_RATIO = 0.75
export const THIN_SOLID = 0.85
/**
 * Live calibration of the glass presets' transmission (clear / frosted / prism / tinted; not Liquid Glass): measured on a
 * #e04030 glyph over a white satin plate (tint 1 / 0.35, frosted 0.55, tinted), Cycles shows ≈ 0.81 · sqrt(body) + 0.02.
 */
export const GLASS_TRANSMIT = 0.72
/** Live calibration (see IconMaterialSpec.albedoGain): Cycles / three.js face-on response of matte clay (0.893 measured). */
export const MATTE_CLAY_RESPONSE = 0.893
/** scene.RASTER_RIM: rim strength on raster (alpha-traced) pieces (their wobbly contours drew a jagged white fringe). */
export const RASTER_RIM = 0.4
/** materials.py: Liquid Glass rim × (lo → 1, smoothstep over paint lightness L a → b): dark paints keep a faint sheen. */
export const LG_RIM_LIGHTNESS = { from: [0.1, 0.8] as const, to: [0.06, 1.0] as const }

/** materials.py SATIN_DARK ('brand'): satin paints with a peak radiance below this get a proportionally weaker sheen. */
export const SATIN_DARK = 0.08

/**
 * How paints are pre-compensated for the colour mode's view transform (worker display_paint): 'neutral' = inverse
 * Khronos PBR Neutral, 'brand' = inverse highlight soft clip (identity below its knee), 'identity' = as is (Standard,
 * AgX). MaterialContext.displayPaint also takes the round-4 boolean (true = 'neutral').
 */
export type PaintTransform = 'neutral' | 'brand' | 'identity'
export function paintTransformFor(colorMode: string | null | undefined): PaintTransform {
  const cm = colorModeId(colorMode)
  return cm === 'neutral' ? 'neutral' : cm === 'brand' ? 'brand' : 'identity'
}
function asTransform(m: boolean | PaintTransform | undefined): PaintTransform {
  return m === true || m === undefined ? 'neutral' : m === false ? 'identity' : m
}

/** materials.brand_diffuse_peak: brightest displayed target a diffuse surface reaches in 'brand' (soft clip of ALBEDO_MAX + B). */
export function brandDiffusePeak(): number {
  return Math.min(BRAND_CAP, softClip([ALBEDO_MAX + DIFFUSE_B, 0, 0])[0])
}

/** Worker Liquid Glass clear share of the flat cap: LG_CAP_CLEAR (16 %) at the default translucency 0.75, ≤ 60 %. */
export function liquidGlassClearShare(translucency: number): number {
  return Math.min(0.6, LG_CAP_CLEAR * (Math.max(0, translucency) / 0.75) ** 4)
}

/**
 * Worker display_paint / util.pbr_neutral_inverse ('neutral' colour mode): the scene-linear radiance that Khronos PBR
 * Neutral displays as the (linear) paint colour — the 0.04 toe offset added back, peaks above 0.76 un-compressed and
 * re-saturated, the target peak capped by saturation (NEUTRAL_CAP). JS twin of GLSL `bisDisplayPaint`.
 */
export function displayPaint(rgb: RGB, maxPeak: number = NEUTRAL_CAP[0], mode: PaintTransform = 'neutral'): RGB {
  if (mode === 'identity') return rgb
  if (mode === 'brand') {
    // materials._brand_graph: per channel, targets ≤ BRAND_CAP (diffuse surfaces: brand_diffuse_peak)
    const cap = maxPeak >= NEUTRAL_CAP[0] - 1e-9 ? BRAND_CAP : brandDiffusePeak()
    return softClipInverse(rgb, BRAND_KNEE, cap)
  }
  const y0 = rgb.map((v) => Math.max(0, v)) as RGB
  const mx = Math.max(...y0)
  const mn = Math.min(...y0)
  const sat = clamp(1 - mn / Math.max(mx, 1e-5), 0, 1)
  const cap = NEUTRAL_CAP[0] + (NEUTRAL_CAP[1] - NEUTRAL_CAP[0]) * sat ** NEUTRAL_CAP_POW
  const npk = Math.min(mx, cap, maxPeak)
  const y = y0.map((v) => (v * npk) / Math.max(mx, 1e-5)) as RGB
  let x1 = y
  if (npk > PBR_START) {
    const d = 1 - PBR_START
    const peak = PBR_START - d + (d * d) / (1 - npk)
    const inv = PBR_DESAT * (peak - npk) + 1
    const g = 1 - 1 / inv
    x1 = y.map((v) => (Math.max(0, (v - g * npk) * inv) * peak) / Math.max(npk, 1e-5)) as RGB
  }
  const m = Math.min(...x1)
  const off = m >= 0.04 ? 0.04 : 0.4 * Math.sqrt(Math.max(m, 0)) - m
  return x1.map((v) => v + off) as RGB
}

/**
 * Worker paint_radiance: displayPaint for opaque pieces; translucent pieces (opacity / alpha paint) are blended in
 * radiance, so in the 'neutral' mode they use the transform's mid-tone approximation paint + 0.04, peak ≤ 1 ('brand'
 * is the identity below its knee: the exact inverse blends display-linear).
 */
export function paintRadiance(rgb: RGB, translucent: boolean, mode: boolean | PaintTransform = 'neutral'): RGB {
  const t = asTransform(mode)
  if (t === 'neutral' && translucent) return diffuseAlbedo(rgb, -0.04, 1)
  return displayPaint(rgb, NEUTRAL_CAP[0], t)
}

/**
 * Worker b_satin: Specular Tint (paint hue at peak 1) and the albedo of (rad − tint · B) for the paint `rgb`; in 'brand'
 * dark paints (peak radiance < SATIN_DARK) get a proportionally weaker specular and coat (`coat` = the coat factor).
 */
export function satinPaint(rgb: RGB, mode: boolean | PaintTransform = 'neutral'): { albedo: RGB; specularTint: RGB; coat: number } {
  const t = asTransform(mode)
  const rad = displayPaint(rgb, DIFFUSE_PEAK, t)
  const mx = Math.max(Math.max(...rad), 1e-4)
  const dk = t === 'brand' ? 0.12 + 0.88 * clamp(Math.max(...rad) / SATIN_DARK, 0, 1) : 1
  const tint = rad.map((v) => (v / mx) * dk) as RGB
  return { albedo: diffuseAlbedo(rad.map((v, i) => v - tint[i] * DIFFUSE_B) as RGB, 0), specularTint: tint, coat: dk }
}

/** Worker diffuse_paint: the albedo of a diffuse preset (B = ALBEDO_B[preset]) for the paint `rgb`. */
export function diffusePaint(rgb: RGB, b: number = DIFFUSE_B, mode: boolean | PaintTransform = 'neutral'): RGB {
  return diffuseAlbedo(displayPaint(rgb, DIFFUSE_PEAK, asTransform(mode)), b)
}

/** Worker _albedo: the diffuse albedo the calibrated rig lights to radiance `rad` (hue kept when capped at lim). */
export function diffuseAlbedo(rad: RGB, b: number, lim = ALBEDO_MAX): RGB {
  const x = rad.map((v) => Math.max(0, v - b) / DIFFUSE_A) as RGB
  const k = Math.min(lim / Math.max(Math.max(...x), 1e-5), 1)
  return x.map((v) => v * k) as RGB
}
/** Worker Liquid Glass: Specular IOR Level 0.4 on the face (three.js specularIntensity 1 ≙ Blender 0.5). */
export const LG_SPECULAR = 0.8

/** Worker `lit`: the Liquid Glass self-illumination follows the key light. */
export function liquidGlassLit(key: number): number {
  return Math.max(0.4, Math.min(1.4, 0.55 + 0.45 * key))
}

/** Worker `tint_curve`: UI tint (0..1) → colour mix factor, eased out so the default 0.45 keeps colours vivid. */
export function tintCurve(t: number): number {
  const c = Math.max(0, Math.min(1, t))
  return 1 - (1 - c) ** 4
}

/**
 * Mono / tint renditions (PLAN §5/§10, worker `_mono`): perceptual luminance stretched from the icon-wide range to
 * MONO_FLOOR..1 (0.3..1, defined with the other rendition rules in lib/appearance.ts). A narrow range is never
 * over-stretched (lo ≤ hi − MONO_MIN_RANGE), like the worker.
 */
export { MONO_FLOOR, MONO_MIN_RANGE }

/** Preset param defaults merged with the layer's overrides. Unknown presets fall back to `satin`. */
export function resolveMaterialParams(spec: MaterialSpec, presets: Presets | null | undefined): Params {
  const preset = presets?.materials?.[spec.preset]
  const out: Params = {}
  if (preset) for (const [k, schema] of Object.entries(preset.params)) out[k] = schema.default
  return { ...out, ...(spec.params ?? {}) }
}

export function presetPaintMode(presetId: string, presets: Presets | null | undefined): PaintMode {
  const p = presets?.materials?.[presetId]?.paint
  if (p === 'tint' || p === 'base' || p === 'emission') return p
  return FALLBACK_PAINT[presetId] ?? 'base'
}

const FALLBACK_PAINT: Record<string, PaintMode> = {
  liquid_glass: 'tint',
  clear_glass: 'tint',
  frosted_glass: 'tint',
  dispersive_crystal: 'tint',
  tinted_glass: 'tint',
  jelly: 'tint',
  neon: 'emission',
  flat: 'emission',
}

function baseSpec(presetId: string, paintMode: PaintMode, intent: MaterialIntent): IconMaterialSpec {
  return {
    preset: presetId,
    paintMode,
    baseColor: WHITE,
    neutral: WHITE,
    paintMix: 1,
    paintPerceptual: false,
    transmitBody: false,
    roughness: 0.5,
    metalness: 0,
    transmission: 0,
    milk: 0,
    milkByLum: false,
    ior: 1.5,
    thicknessScale: 2.4,
    clearcoat: 0,
    clearcoatRoughness: 0.05,
    specularIntensity: 1,
    envMapIntensity: 1,
    sheen: 0,
    sheenRoughness: 0.5,
    sheenTint: 0,
    iridescence: 0,
    iridescenceIOR: 1.6,
    iridescenceRange: [250, 900],
    iridescenceBands: 3,
    dispersion: 0,
    anisotropy: 0,
    anisotropyRadial: false,
    grain: 0,
    attenuationMix: 0,
    attenuationDistanceScale: Infinity,
    emissive: 0,
    whiteMilk: 0,
    albedoB: null,
    albedoGain: 1,
    displayEmission: false,
    specularTint: false,
    core: 0,
    rim: { key: 0, back: 0, glow: 0 },
    lg: null,
    bloom: 0,
    unlit: false,
    intent,
  }
}

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

export function describeMaterial(spec: MaterialSpec, presets: Presets | null | undefined): IconMaterialSpec {
  const known = !presets || !!presets.materials?.[spec.preset] || spec.preset in FALLBACK_PAINT
  const presetId = known ? spec.preset : 'satin'
  const params = resolveMaterialParams({ preset: presetId, params: spec.params }, presets)
  const num = (k: string, d: number): number => {
    const v = params[k]
    return typeof v === 'number' && Number.isFinite(v) ? v : d
  }
  const str = (k: string, d: string): string => (typeof params[k] === 'string' ? (params[k] as string) : d)
  const intent = readMaterialIntent(spec)
  const s = baseSpec(presetId, presetPaintMode(presetId, presets), intent)

  // Glass presets take their colour like the worker's glass_colors(): mix(white, paint, tintCurve(tint)) in
  // perceptual space (three.js transmission multiplies by the base colour once ≈ Blender's two interfaces × sqrt).
  const glassTint = (d: number) => {
    s.neutral = WHITE
    s.paintMix = tintCurve(num('tint', d))
    s.paintPerceptual = true
  }

  switch (presetId) {
    case 'liquid_glass': {
      const specular = str('specular', 'auto')
      const transl = clamp(num('translucency', 0.75), 0, 1)
      const tCap = liquidGlassClearShare(transl)
      glassTint(1.0)
      s.transmission = 1
      s.roughness = num('frost', 0.06)
      s.ior = num('ior', 1.5)
      s.thicknessScale = 2.8
      s.clearcoat = specular === 'off' ? 0 : 1
      s.clearcoatRoughness = 0.02
      s.specularIntensity = LG_SPECULAR
      s.lg = {
        fill: [1 - tCap, 1 - 0.35 * tCap],
        rim: specular === 'off' ? 0 : 5 * num('rim', 1),
        band: LG_RIM_BANDS[specular] ?? LG_RIM_BANDS.auto,
        glow: num('glow', 0.35),
        edgeDark: clamp(intent.edgeDark, 0, 1),
      }
      break
    }
    case 'clear_glass':
      glassTint(0.1)
      s.whiteMilk = CLEAR_WHITE_MILK
      s.transmission = 1
      s.roughness = num('frost', 0)
      s.ior = num('ior', 1.5)
      s.thicknessScale = 2.4
      // worker _glass_common(coat=1.0, coat_rough=0.01): a glossy coat + Specular IOR Level 0.6 (three.js 1.2)
      s.clearcoat = 1
      s.clearcoatRoughness = 0.01
      s.specularIntensity = 1.2
      s.rim = { key: 0.25, back: 0.1, glow: 0 }
      break
    case 'frosted_glass':
      glassTint(0.25)
      s.whiteMilk = WHITE_ICE
      s.transmission = 1
      s.roughness = num('frost', 0.35)
      s.ior = num('ior', 1.5)
      s.grain = num('grain', 0.08)
      s.clearcoat = num('coat', 1)
      s.clearcoatRoughness = 0.03
      s.thicknessScale = 2.2
      s.rim = { key: 0.2, back: 0.08, glow: 0 }
      break
    case 'dispersive_crystal': {
      const ior = num('ior', 1.6)
      glassTint(0.05)
      s.transmitBody = true // worker: Glass BSDF lobes (colour per interface), not Principled
      s.whiteMilk = CLEAR_WHITE_MILK
      s.transmission = 1
      s.roughness = num('frost', 0)
      s.ior = ior
      // Preset dispersion = IOR spread of the R/B lobes (Blender 3-lobe trick); three: halfSpread = (ior−1)·0.025·D.
      s.dispersion = num('dispersion', 0.06) / Math.max((ior - 1) * 0.025, 1e-3)
      s.thicknessScale = 3.4
      s.specularIntensity = 1.1
      s.rim = { key: 0.3, back: 0.12, glow: 0 }
      break
    }
    case 'tinted_glass': {
      const absorption = num('absorption', 0)
      glassTint(0.95)
      s.transmission = 1
      s.roughness = num('frost', 0.04)
      s.ior = num('ior', 1.5)
      s.thicknessScale = 2.6
      if (absorption > 0.01) {
        s.attenuationMix = 1
        s.attenuationDistanceScale = 3 / absorption
      }
      s.rim = { key: 0.2, back: 0.1, glow: 0 }
      break
    }
    case 'jelly': {
      const density = num('density', 8)
      const scatter = num('scatter', 4)
      s.neutral = WHITE
      s.paintMix = 0.3
      s.transmission = clamp(1 - 0.035 * scatter, 0.55, 1)
      s.roughness = num('frost', 0.08)
      s.ior = num('ior', 1.35)
      s.thicknessScale = 2.6
      s.attenuationMix = 1
      s.attenuationDistanceScale = (8 / Math.max(density, 0.2)) * 0.6
      s.emissive = 0.025 * scatter
      s.sheen = 0.25
      s.sheenTint = 1
      s.clearcoat = 0.5
      s.clearcoatRoughness = 0.05
      s.rim = { key: 0.24, back: 0.1, glow: 0 }
      break
    }
    // Rim strengths below mirror the worker's _rim(strength) on the solid presets (≈ strength / 5).
    case 'glossy_plastic':
      s.albedoB = ALBEDO_B.glossy_plastic
      s.roughness = num('roughness', 0.35)
      s.clearcoat = num('coat', 1)
      s.clearcoatRoughness = num('coatRoughness', 0.03)
      s.rim = { key: 0.07, back: 0.03, glow: 0 }
      break
    case 'satin':
      // Worker b_satin: Specular IOR Level 0.35 (three.js specularIntensity 1 ≙ Blender 0.5), sheen 0.03 — kept low so
      // the mirrored studio world does not wash saturated plates out; the coat (presets.json default 0.15) carries
      // the gloss.
      s.albedoB = ALBEDO_B.satin
      s.specularTint = true
      s.roughness = num('roughness', 0.45)
      s.clearcoat = num('coat', 0.15)
      s.clearcoatRoughness = 0.06
      s.specularIntensity = 0.7
      s.sheen = 0.03
      s.sheenRoughness = 0.5
      break
    case 'candy': {
      const sss = num('subsurface', 1)
      const trans = num('transmission', 0)
      s.albedoB = ALBEDO_B.candy
      s.roughness = num('roughness', 0.15)
      s.clearcoat = num('coat', 1)
      s.clearcoatRoughness = 0.02
      s.emissive = 0.16 * sss // light scattered inside the sugar
      s.sheen = 0.25 * sss
      s.sheenTint = 1
      s.sheenRoughness = 0.35
      s.rim = { key: 0.1, back: 0.045, glow: 0 }
      if (trans > 0.01) {
        s.transmission = trans
        s.attenuationMix = 1
        s.attenuationDistanceScale = 1.5
        s.thicknessScale = 2.2
        s.ior = 1.45
      }
      break
    }
    case 'gummy': {
      const soft = num('subsurfaceScale', 0.3)
      s.albedoB = ALBEDO_B.gummy
      s.roughness = num('roughness', 0.2)
      s.clearcoat = num('coat', 0.6)
      s.clearcoatRoughness = 0.05
      s.emissive = 0.1 + 0.18 * soft
      s.sheen = 0.6
      s.sheenTint = 1
      s.sheenRoughness = 0.45
      s.rim = { key: 0.08, back: 0.035, glow: 0 }
      break
    }
    case 'chrome':
      s.metalness = 1
      s.neutral = [0.92, 0.93, 0.95]
      s.paintMix = num('tint', 0)
      s.roughness = num('roughness', 0.05)
      s.envMapIntensity = 1.15
      break
    case 'brushed_metal': {
      const film = num('film', 0)
      s.metalness = 1
      s.neutral = [0.8, 0.81, 0.83]
      s.paintMix = num('tint', 1)
      s.roughness = num('roughness', 0.3)
      s.anisotropy = num('anisotropy', 0.8)
      s.anisotropyRadial = str('brush', 'radial') === 'radial'
      if (film > 5) {
        s.iridescence = 1
        s.iridescenceIOR = 2.2
        s.iridescenceRange = [film * 0.75, film * 1.25]
        s.iridescenceBands = 1
      }
      break
    }
    case 'matte_clay':
      s.albedoB = ALBEDO_B.matte_clay
      s.albedoGain = MATTE_CLAY_RESPONSE
      s.neutral = [0.8, 0.76, 0.72]
      s.paintMix = num('tint', 1)
      s.roughness = num('roughness', 0.9)
      s.specularIntensity = 0.3
      s.sheen = num('sheen', 0.15)
      s.sheenRoughness = 0.8
      s.sheenTint = 0.3
      break
    case 'iridescent':
      s.neutral = [0.9, 0.9, 0.92]
      s.paintMix = num('tint', 0.2)
      s.metalness = num('metallic', 1)
      s.roughness = num('roughness', 0.15)
      s.iridescence = 1
      s.iridescenceIOR = num('filmIor', 1.6)
      s.iridescenceRange = [num('filmMin', 250), Math.max(num('filmMin', 250) + 1, num('filmMax', 900))]
      s.iridescenceBands = num('bands', 3)
      break
    case 'neon':
      // Worker b_neon: strength 4 → emission 1.2 (saturated under Khronos PBR Neutral / AgX); the glow comes from
      // bloom on the emissive tubes, the hot core mixes 0.6 × core toward white where the tube faces the camera.
      s.baseColor = [0.03, 0.03, 0.035]
      s.roughness = 0.25
      s.clearcoat = 0.6
      s.clearcoatRoughness = 0.05
      s.emissive = 0.3 * num('strength', 4)
      s.core = 0.6 * num('core', 0.25)
      s.bloom = num('bloom', 0.6)
      break
    case 'flat':
      s.baseColor = [0, 0, 0]
      s.roughness = 1
      s.specularIntensity = 0
      s.envMapIntensity = 0
      s.emissive = 1
      s.displayEmission = true
      s.unlit = true
      break
    default:
      s.roughness = num('roughness', 0.45)
      s.clearcoat = num('coat', 0.25)
      s.clearcoatRoughness = 0.12
  }

  // Appearance intents (PLAN §5). The material params themselves were already rewritten by resolveAppearance
  // (exactly like the worker's appearance.py); here only the per-pixel parts remain.
  // Clear renditions: the milky (frosted) fraction follows the stretched mono luminance (worker: milk × st × 1.3).
  if (intent.intent === 'mono' && s.paintMode === 'tint') s.milkByLum = true
  // Tinted-dark: extra paint-coloured emission (worker env.emission → Emission strength on the mono × tint paint).
  if (intent.emissive > 0 && s.paintMode !== 'emission') s.emissive += intent.emissive
  return s
}

// ================================================================================================ IconMaterial

/** Affine art → paint-map UV: u = a·x + b·y + c, v = d·x + e·y + f. */
export type PaintUv = readonly [number, number, number, number, number, number]
/** The layer textures cover the art square −1..1: u = (x + 1) / 2, v = (y + 1) / 2. */
export const ART_SQUARE_UV: PaintUv = [0.5, 0, 0.5, 0, 0.5, 0.5]

export interface PaintBinding {
  /** Layer texture / fill gradient (sRGB texture, art-space UVs) or null → `color`. */
  map: THREE.Texture | null
  /** How object (art) XY maps onto `map` (default ART_SQUARE_UV; raster cards use their image placement). */
  uv?: PaintUv
  /** Linear colour used when there is no map (solid fills, loading). */
  color: THREE.Color
  /** Linear luminance range of the paint (mono / tint renditions). */
  lumRange: [number, number]
  /** Largest alpha of the paint map (default 1). */
  alphaMax?: number
  /**
   * Mono / tint renditions (round 5, worker `_mono` with a `lut`): perceptual lightness → mono level as a colour-ramp
   * texture (monoLutTexture); null = the linear lumRange stretch.
   */
  monoLut?: THREE.Texture | null
}

/** Samples of a mono LUT texture (linear filtering between them: a piecewise-linear ramp like Blender's ValToRGB). */
export const MONO_LUT_SIZE = 256
const lutTextures = new Map<string, THREE.DataTexture>()
/**
 * A worker mono `lut` (MonoLut colour-ramp stops) as a MONO_LUT_SIZE × 1 half-float texture, sampled at lightness l by
 * u = (l · (N − 1) + 0.5) / N. Cached by content (a handful per session; never disposed).
 */
export function monoLutTexture(lut: MonoLut): THREE.DataTexture {
  const key = JSON.stringify(lut.map(([x, y]) => [+x.toFixed(6), +y.toFixed(6)]))
  let t = lutTextures.get(key)
  if (!t) {
    const data = new Uint16Array(MONO_LUT_SIZE * 4)
    for (let i = 0; i < MONO_LUT_SIZE; i++) {
      const v = THREE.DataUtils.toHalfFloat(lutValue(lut, i / (MONO_LUT_SIZE - 1)))
      data.set([v, v, v, THREE.DataUtils.toHalfFloat(1)], i * 4)
    }
    t = new THREE.DataTexture(data, MONO_LUT_SIZE, 1, THREE.RGBAFormat, THREE.HalfFloatType)
    t.minFilter = THREE.LinearFilter
    t.magFilter = THREE.LinearFilter
    t.wrapS = THREE.ClampToEdgeWrapping
    t.wrapT = THREE.ClampToEdgeWrapping
    t.generateMipmaps = false
    t.colorSpace = THREE.NoColorSpace
    t.needsUpdate = true
    if (lutTextures.size > 64) lutTextures.clear()
    lutTextures.set(key, t)
  }
  return t
}

/**
 * Raster regions whose paint never gets more than half opaque (shading / highlight overlays traced from soft-alpha
 * images) render without refraction: the worker alpha-mixes its glass with transparency, so at ≤ 50 % alpha the
 * body reads as a tinted veil — three.js transmission instead refracts the traced polygon's bevels into shards and,
 * being drawn after the blended layers, would hide the translucent art underneath it.
 */
export const OVERLAY_ALPHA_MAX = 0.5

export interface FakeGlassBinding {
  /** What the glass shows through itself: the plate fill / wallpaper (canvas space) or the backdrop (screen space). */
  map: THREE.Texture | null
  color: THREE.Color
  space: 'canvas' | 'screen'
  /** Canvas space only: the map covers world x, y ∈ ±extent (default 1 = the plate square). */
  extent?: number
  /** The map is the rendition wallpaper of this tone (worker WALLPAPERS). */
  tone?: 'light' | 'dark'
  /**
   * A glass PLATE over the wallpaper (worker role 'backdrop', its EEVEE stand-in `_backdrop_glass`, which matches the
   * Cycles render): the wallpaper straight below × the glass colour, desaturated toward grey on the dark wallpaper,
   * whitened by the frost's forward scatter (screen with (0.1 + 0.35 · frost) · (dark ? 0.32 : 1)), as emission over a
   * black, coated dielectric. Needs `tone`.
   */
  backdropGlass?: boolean
}

export interface MaterialContext {
  paint: PaintBinding
  /** Body thickness in the mesh's local units (three.js scales transmission thickness by the model scale). */
  thickness: number
  /** Uniform local → world scale of the mesh (attenuation distances are world units). Default 1. */
  modelScale?: number
  /** Non-null → render glass presets as opaque "fake glass" (glass under other glass, PLAN D7). */
  fake: FakeGlassBinding | null
  /**
   * Multiply opacity by the paint map's alpha (raster-image regions: the worker projects the image with its alpha,
   * so soft / partly transparent traced pixels stay see-through). Ignored without a paint map.
   */
  paintAlpha?: boolean
  /** World-space unit vector toward the key light (rim mask). */
  rimDir: THREE.Vector3
  rimColor?: THREE.Color
  /** Front-face dome (LayerDepth.inflate) about `center` with `radius` (art units). */
  inflate?: number
  center?: [number, number]
  radius?: number
  /** Art-space y extent of the layer (translucency falloff). */
  milkRange?: [number, number]
  opacity: number
  /** Liquid Glass self-illumination factor (liquidGlassLit(rig key)); default 1. */
  lit?: number
  /**
   * Paint pre-compensation for the project's colour mode (worker display_paint, see PaintTransform): 'neutral' (or
   * true) = inverse Khronos PBR Neutral, 'brand' = inverse highlight soft clip, false / 'identity' = none. Default false.
   */
  displayPaint?: boolean | PaintTransform
  /** The icon plate (worker spec 'plate'): no white-ice / white-milk body. */
  plate?: boolean
  /** Liquid Glass 'solid_edge' (thin strokes, THIN_SOLID): body share floor across the bevel. Default 0. */
  solidEdge?: number
  /** Liquid Glass art flush with the plate outline (worker spec 'flush', lib/overlay3d.flushSpec). */
  flush?: FlushSpec | null
  /** Rim strength factor (raster pieces: RASTER_RIM). Default 1. */
  rimScale?: number
  /**
   * Translucent Liquid Glass piece rendered as a display-space blend FILM (worker _film / overlay.py): the piece keeps
   * `t` of the radiance beneath it (blend colour) and adds `e` (+ its coat sheen, rim and glow). Null = alpha model.
   */
  film?: FilmParams | null
}

/** Worker spec 'flush' (overlay.flush_spec): plate outline field + the band (canvas units) where the rim fades out. */
export interface FlushSpec {
  shape: string
  r: number
  grow: number
  band: [number, number]
}
/** Per-channel film transmission / emission (radiance domain, overlay.film_params). */
export interface FilmParams {
  t: RGB
  e: RGB
}

function createUniforms() {
  return {
    bisPaintMap: { value: null as THREE.Texture | null },
    bisPaintUv: { value: new THREE.Matrix3().set(0.5, 0, 0.5, 0, 0.5, 0.5, 0, 0, 1) },
    bisPaintColor: { value: new THREE.Color(1, 1, 1) },
    bisPaintMix: { value: 1 },
    bisNeutral: { value: new THREE.Color(1, 1, 1) },
    bisLumRange: { value: new THREE.Vector2(0, 1) },
    bisMonoLut: { value: null as THREE.Texture | null },
    bisIntentTint: { value: new THREE.Color(1, 1, 1) },
    bisIntentStrength: { value: 0 },
    bisEmissive: { value: 0 },
    bisCore: { value: 0 },
    bisAttenuationMix: { value: 0 },
    bisRimDir: { value: new THREE.Vector3(0, 0.7, 0.7) },
    bisRimColor: { value: new THREE.Color(1, 1, 1) },
    bisRimKey: { value: 0 },
    bisRimBack: { value: 0 },
    bisGlow: { value: 0 },
    bisBehindMap: { value: null as THREE.Texture | null },
    bisBehindColor: { value: new THREE.Color(1, 1, 1) },
    bisBehindLod: { value: 0 },
    bisBehindScale: { value: 0.5 },
    bisFakeTransmit: { value: 0 },
    bisFakeRefract: { value: 0 },
    bisBdScatter: { value: 0 },
    bisBdDesat: { value: 0 },
    bisMilk: { value: 0 },
    bisMilkRange: { value: new THREE.Vector2(-1, 1) },
    bisCenter: { value: new THREE.Vector2() },
    bisRadius: { value: 1 },
    bisInflate: { value: 0 },
    bisLgFill: { value: new THREE.Vector2(0.5, 0.9) },
    bisLgRim: { value: 0 },
    bisLgBand: { value: new THREE.Vector4(0.015, 0.06, 0.24, 0.4) },
    bisLgGlow: { value: 0 },
    bisLgLit: { value: 1 },
    bisLgEdgeDark: { value: 0 },
    bisAlbedoB: { value: 0 },
    bisAlbedoGain: { value: 1 },
    bisWhiteMilk: { value: 0 },
    bisLgSolidEdge: { value: 0 },
    bisFlush: { value: new THREE.Vector4(1, 0.45, 0, 0) },
    bisFlushBand: { value: new THREE.Vector2(-3, -2) },
    bisFilmE: { value: new THREE.Vector3() },
    bisFilmAlpha: { value: 1 },
  }
}

export type IconUniforms = ReturnType<typeof createUniforms>

const VERTEX_PARS = /* glsl */ `
varying vec2 vBisArt;
varying vec3 vBisWorld;
varying vec4 vBisClip;
#ifdef BIS_INFLATE
  uniform vec2 bisCenter;
  uniform float bisRadius;
  uniform float bisInflate;
  varying vec3 vBisTilt;
#endif
`

const VERTEX_MAIN = /* glsl */ `
#include <project_vertex>
vBisArt = position.xy;
vBisWorld = ( modelMatrix * vec4( transformed, 1.0 ) ).xyz;
vBisClip = gl_Position;
#ifdef BIS_INFLATE
  float bisCap = smoothstep( 0.97, 0.995, objectNormal.z );
  vBisTilt = normalMatrix * vec3( ( position.xy - bisCenter ) / max( bisRadius, 1e-4 ) * bisInflate * bisCap, 0.0 );
#endif
`

const glslNum = (v: number) => (Number.isInteger(v) ? v.toFixed(1) : String(v))
const NEUTRAL_CAP_GLSL = NEUTRAL_CAP.map(glslNum)
const PBR_START_GLSL = glslNum(PBR_START)
const PBR_DESAT_GLSL = glslNum(PBR_DESAT)
const DIFFUSE_A_GLSL = glslNum(DIFFUSE_A)
const ALBEDO_MAX_GLSL = glslNum(ALBEDO_MAX)
const LG_COAT_B_GLSL = glslNum(LG_COAT_B)
const BRAND_KNEE_GLSL = glslNum(BRAND_KNEE)
const BRAND_W_GLSL = (1 - BRAND_KNEE).toFixed(6)

const FRAGMENT_PARS = /* glsl */ `
uniform vec3 bisPaintColor;
uniform float bisPaintMix;
uniform vec3 bisNeutral;
uniform float bisEmissive;
uniform float bisCore;
uniform float bisAttenuationMix;
uniform vec3 bisRimDir;
uniform vec3 bisRimColor;
uniform float bisRimKey;
uniform float bisRimBack;
uniform float bisGlow;
uniform float bisMilk;
uniform vec2 bisMilkRange;
uniform float bisAlbedoB;
uniform float bisAlbedoGain;
uniform float bisWhiteMilk;
varying vec2 vBisArt;
varying vec3 vBisWorld;
varying vec4 vBisClip;
#ifdef BIS_PAINT_MAP
  uniform sampler2D bisPaintMap;
  uniform mat3 bisPaintUv;
  vec2 bisPaintCoord() { return ( bisPaintUv * vec3( vBisArt, 1.0 ) ).xy; }
#endif
#if BIS_INTENT > 0
  uniform vec2 bisLumRange;
  uniform vec3 bisIntentTint;
  uniform float bisIntentStrength;
  #ifdef BIS_MONO_LUT
    uniform sampler2D bisMonoLut;
  #endif
#endif
#ifdef BIS_FAKE_GLASS
  uniform vec3 bisBehindColor;
  uniform float bisBehindLod;
  uniform float bisBehindScale;
  uniform float bisFakeTransmit;
  uniform float bisFakeRefract;
  uniform float bisBdScatter;
  uniform float bisBdDesat;
  #ifdef BIS_BEHIND_MAP
    uniform sampler2D bisBehindMap;
  #endif
#endif
#ifdef BIS_INFLATE
  varying vec3 vBisTilt;
#endif
#ifdef BIS_LG
  uniform vec2 bisLgFill;
  uniform float bisLgRim;
  uniform vec4 bisLgBand;
  uniform float bisLgGlow;
  uniform float bisLgLit;
  uniform float bisLgEdgeDark;
  uniform float bisLgSolidEdge;
  vec3 bisRad = vec3( 1.0 );
  #ifdef BIS_FILM
    uniform vec3 bisFilmE;
    uniform float bisFilmAlpha;
  #endif
  #ifdef BIS_FLUSH
    uniform vec4 bisFlush;     // grow, R (rounded-rect radius), squircle (0 / 1), -
    uniform vec2 bisFlushBand; // B0, B1
    // Worker _flush_keep / _flush_graph: smoothstep(B0, B1, distance to the plate outline) — rounded-rect SDF or the
    // squircle field 1 − (|x|^5 + |y|^5)^(1/5), in units of grow; world XY == canvas XY.
    float bisFlushKeep() {
      float grow = max( bisFlush.x, 1e-3 );
      vec2 q = abs( vBisWorld.xy / grow );
      float r = bisFlush.y;
      vec2 qq = q - 1.0 + r;
      float dR = r - ( length( max( qq, vec2( 0.0 ) ) ) + min( max( qq.x, qq.y ), 0.0 ) );
      float dS = 1.0 - pow( pow( q.x, 5.0 ) + pow( q.y, 5.0 ), 0.2 );
      return smoothstep( bisFlushBand.x, bisFlushBand.y, mix( dR, dS, bisFlush.z ) * grow );
    }
  #endif
  // Worker _band: smoothstep(a0, a1, e) · (1 − smoothstep(b0, b1, e)).
  float bisBand( vec4 b, float e ) { return smoothstep( b.x, b.y, e ) * ( 1.0 - smoothstep( b.z, b.w, e ) ); }
#endif
// Worker _whiteness: perceptual HSV value × (1 − saturation).
float bisWhiteness( vec3 c ) {
  float mx = max( max( c.r, c.g ), c.b );
  float mn = min( min( c.r, c.g ), c.b );
  float sat = mx > 1e-6 ? ( mx - mn ) / mx : 0.0;
  return clamp( pow( max( mx, 0.0 ), 1.0 / 2.2 ) * ( 1.0 - sat ), 0.0, 1.0 );
}
// Worker display_paint (colour mode 'neutral'): radiance that Khronos PBR Neutral displays as the paint (displayPaint()).
vec3 bisDisplayPaint( vec3 c, float maxPeak ) {
  #ifdef BIS_DISPLAY_PAINT
    vec3 y0 = max( c, vec3( 0.0 ) );
    float mx = max( max( y0.r, y0.g ), y0.b );
    float mn = min( min( y0.r, y0.g ), y0.b );
    float sat = clamp( 1.0 - mn / max( mx, 1e-5 ), 0.0, 1.0 );
    float cap = ${NEUTRAL_CAP_GLSL[0]} + ( ${NEUTRAL_CAP_GLSL[1]} - ${NEUTRAL_CAP_GLSL[0]} ) * pow( sat, ${glslNum(NEUTRAL_CAP_POW)} );
    float npk = min( mx, min( cap, maxPeak ) );
    vec3 y = y0 * ( npk / max( mx, 1e-5 ) );
    vec3 x1 = y;
    if ( npk > ${PBR_START_GLSL} ) {
      float d = 1.0 - ${PBR_START_GLSL};
      float peak = ${PBR_START_GLSL} - d + d * d / ( 1.0 - npk );
      float inv = ${PBR_DESAT_GLSL} * ( peak - npk ) + 1.0;
      float g = 1.0 - 1.0 / inv;
      x1 = max( ( y - g * npk ) * inv, vec3( 0.0 ) ) * ( peak / max( npk, 1e-5 ) );
    }
    float m = min( min( x1.r, x1.g ), x1.b );
    float off = m > 0.04 ? 0.04 : 0.4 * sqrt( max( m, 0.0 ) ) - m;
    return x1 + off;
  #elif defined( BIS_BRAND_PAINT )
    // Worker _brand_graph ('brand'): the inverse highlight soft clip, per channel, identity below the knee; targets
    // ≤ BRAND_CAP (diffuse surfaces, maxPeak < the neutral cap: brand_diffuse_peak).
    float cap = maxPeak >= ${glslNum(NEUTRAL_CAP[0] - 1e-6)} ? ${glslNum(BRAND_CAP)} : ${glslNum(brandDiffusePeak())};
    vec3 y = clamp( c, vec3( 0.0 ), vec3( cap ) );
    vec3 hi = ${BRAND_KNEE_GLSL} - ${BRAND_W_GLSL} * log( 1.0 - max( y - ${BRAND_KNEE_GLSL}, vec3( 0.0 ) ) / ${BRAND_W_GLSL} );
    return mix( y, hi, step( vec3( ${BRAND_KNEE_GLSL} ), y ) );
  #else
    return c;
  #endif
}
vec3 bisDisplayPaint( vec3 c ) { return bisDisplayPaint( c, ${NEUTRAL_CAP_GLSL[0]} ); }
// Worker _albedo: the albedo the calibrated rig lights to radiance rad (DIFFUSE_A · albedo + b), hue kept at the cap.
vec3 bisAlbedoLim( vec3 rad, float b, float lim ) {
  vec3 x = max( rad - b, vec3( 0.0 ) ) / ${DIFFUSE_A_GLSL};
  float mx = max( max( x.r, x.g ), x.b );
  return x * min( lim / max( mx, 1e-5 ), 1.0 );
}
vec3 bisAlbedo( vec3 rad, float b ) { return bisAlbedoLim( rad, b, ${ALBEDO_MAX_GLSL} ); }
// Worker paint_radiance: display_paint for opaque pieces; translucent ones (opacity / alpha paint, BIS_SRGB_ALPHA) blend
// in radiance, so they take the transform's mid-tone approximation paint + 0.04, peak ≤ 1 (paintRadiance()).
vec3 bisPaintRadiance( vec3 c ) {
  #if defined( BIS_DISPLAY_PAINT ) && defined( BIS_SRGB_ALPHA )
    return bisAlbedoLim( c, -0.04, 1.0 );
  #else
    return bisDisplayPaint( c );
  #endif
}
// Stretched mono level of the last paint sample (worker _lum, clear renditions).
float bisLumSt = 1.0;
// Worker b_satin Specular Tint (paint hue, peak 1) — applied to the dielectric F0 after lights_physical_fragment — and
// its 'brand' coat factor (dark paints: a proportionally weaker sheen).
vec3 bisSpecTint = vec3( 1.0 );
float bisSatinCoat = 1.0;
// Extra tint of the transmitted (refracted / fake-glass) light on top of the diffuse colour (Liquid Glass: deeper toward
// the outline, clear-light edge darkening; the diffuse lobe keeps the body colour, like the worker's fill Principled).
vec3 bisTransTint = vec3( 1.0 );
// Worker _lightness: perceptual lightness of a (linear) paint colour, luminance^(1/2.2) clamped to 0..1.
float bisLightness( vec3 c ) {
  return min( pow( max( dot( c, vec3( 0.2126, 0.7152, 0.0722 ) ), 0.0 ), 1.0 / 2.2 ), 1.0 );
}

vec3 bisPaintSample() {
  #ifdef BIS_PAINT_MAP
    vec3 c = texture2D( bisPaintMap, bisPaintCoord() ).rgb;
  #else
    vec3 c = bisPaintColor;
  #endif
  #if BIS_INTENT > 0
    // Worker _mono(): perceptual luminance (RGB→BW, ^1/2.2) mapped from the icon-wide perceptual range
    // [lo, hi] (lo ≤ hi − MONO_MIN_RANGE) onto MONO_FLOOR..1, back to linear grey.
    float l = pow( max( dot( c, vec3( 0.2126, 0.7152, 0.0722 ) ), 0.0 ), 1.0 / 2.2 );
    #ifdef BIS_MONO_LUT
      // Round 5 (worker mono lut): a colour ramp over min(l, 1) — gamma map (clear) or rank spread (tinted, combined).
      float st = texture2D( bisMonoLut, vec2( min( l, 1.0 ) * ${((MONO_LUT_SIZE - 1) / MONO_LUT_SIZE).toFixed(8)} + ${(0.5 / MONO_LUT_SIZE).toFixed(8)}, 0.5 ) ).r;
    #else
      float hi = bisLumRange.y;
      float lo = min( bisLumRange.x, hi - ${MONO_MIN_RANGE.toFixed(4)} );
      float st = mix( ${MONO_FLOOR.toFixed(4)}, 1.0, clamp( ( l - lo ) / max( hi - lo, 1e-4 ), 0.0, 1.0 ) );
    #endif
    bisLumSt = st;
    vec3 g = vec3( pow( st, 2.2 ) );
    #if BIS_INTENT == 2
      g = mix( g, g * bisIntentTint, bisIntentStrength );
    #endif
    c = g;
  #endif
  return c;
}

// Translucency falloff: transparent at the bottom of the layer, milky (paint-coloured) at the top.
float bisMilkAmount( vec3 paint ) {
  float h = clamp( ( vBisArt.y - bisMilkRange.x ) / max( bisMilkRange.y - bisMilkRange.x, 1e-4 ), 0.0, 1.0 );
  float m = mix( 0.45, 1.0, h ) * bisMilk;
  #ifdef BIS_MILK_LUM
    m *= clamp( pow( dot( paint, vec3( 0.2126, 0.7152, 0.0722 ) ), 1.0 / 2.2 ) * 1.3, 0.0, 1.0 );
  #elif defined( BIS_WHITE_MILK )
    // Worker _glass_common(white_milk): white paints tint nothing, so as water-clear glass a white glyph vanished
    // into the plate it lenses — they become frosted ("ice"): milk ≥ whiteness^1.5 · white_milk · (0.8 → 1 up).
    m = max( m, pow( bisWhiteness( paint ), 1.5 ) * bisWhiteMilk * mix( 0.8, 1.0, h ) );
  #endif
  // The worker mixes milk / clear at EVERY surface a camera ray meets: through a slab's two faces only (1 − m)² stays
  // clear (Discord's white crystal glyph: ~75 % white for m ≈ 0.5); three.js shades the front face only.
  m = clamp( m, 0.0, 1.0 );
  return 1.0 - ( 1.0 - m ) * ( 1.0 - m );
}
`

const PAINT_APPLY = /* glsl */ `
#include <map_fragment>
vec3 bisP = bisPaintSample();
// Body colour (worker glass_colors base_m): tinted() = mix(white, paint, tintCurve(tint)) in gamma-2.2 space.
#ifdef BIS_PAINT_PERCEPTUAL
  vec3 bisPm = pow( mix( pow( bisNeutral, vec3( 1.0 / 2.2 ) ), pow( max( bisP, vec3( 0.0 ) ), vec3( 1.0 / 2.2 ) ), bisPaintMix ), vec3( 2.2 ) );
#else
  vec3 bisPm = mix( bisNeutral, bisP, bisPaintMix );
#endif
#ifdef BIS_LG
  // Liquid Glass fill share (see LiquidGlassModel); e = 1 − |N.xy| of the unperturbed normal.
  vec3 bisN0 = inverseTransformDirection( normalize( vNormal ), viewMatrix );
  float bisE = clamp( 1.0 - length( bisN0.xy ), 0.0, 1.0 );
  float bisV01 = clamp( ( vBisArt.y - bisMilkRange.x ) / max( bisMilkRange.y - bisMilkRange.x, 1e-4 ), 0.0, 1.0 );
  // Paint lightness (dark paints read as dark smoked glass) and whiteness (white glyphs: a dense frosted-white body
  // with a gentler vertical falloff).
  float bisLum = bisLightness( bisP );
  float bisWh = pow( bisWhiteness( bisP ), 1.5 );
  // Round 5: near-black paints (nb 0 → 1 over lightness ${LG_NEAR_BLACK[0]} → ${LG_NEAR_BLACK[1]}) are opaque smoked glass;
  // art flush with the plate (bisKeep 0 along the plate outline) has no rim / sheen / glow / clear edge there.
  float bisNb = clamp( ( bisLum - ${glslNum(LG_NEAR_BLACK[0])} ) / ${glslNum(LG_NEAR_BLACK[1] - LG_NEAR_BLACK[0])}, 0.0, 1.0 );
  #ifdef BIS_FLUSH
    float bisKeep = bisFlushKeep();
  #else
    float bisKeep = 1.0;
  #endif
  float bisFill = mix( bisLgFill.x, bisLgFill.y, bisWh ) * mix( 0.94, 1.0, bisV01 );
  #if BIS_INTENT == 1
    bisFill = clamp( bisFill * bisLumSt * 1.15, 0.0, 1.0 );
  #endif
  bisFill = clamp( bisFill * mix( ${glslNum(LG_EDGE_BODY)}, 1.0, smoothstep( 0.0, 0.45, bisE ) ), 0.0, 1.0 );
  bisFill = max( max( bisFill, 1.0 - bisKeep ), max( 1.0 - bisNb, bisLgSolidEdge ) );
  // Coat / specular weight (worker coat_b / spec_b): faded out toward the silhouette, subdued for dark paints, near-black
  // paints keep it on the cap only, none along a flush edge.
  float bisTame = clamp( ( bisE - 0.08 ) / 0.52, 0.0, 1.0 ) * mix( 0.35, 1.0, clamp( bisLum / 0.5, 0.0, 1.0 ) ) * bisKeep;
  float bisCoatNb = mix( clamp( ( bisE - 0.6 ) / 0.3, 0.0, 1.0 ) * 0.5, 1.0, bisNb );
  float bisMilkV = bisFill;
#else
  float bisMilkV = bisMilkAmount( bisP );
#endif
#if BIS_PAINT_MODE != 2
  #ifdef BIS_LG
    // Body = the displayed paint radiance (worker rad = display_paint(base_m)); the diffuse share of the self-lit fill
    // takes the albedo of (rad − coat reflection). The clear share is tinted t2^(2γ), t2 = rad / ALBEDO_MAX (worker:
    // t2^γ per interface, γ 0.9 → 0.5 over e 0 → 0.6; three.js tints a transmitted path once, hence 2γ), so over a
    // white plate it, too, shows the paint. bisTransTint is relative to the diffuse colour three.js multiplies in.
    bisRad = bisPaintRadiance( bisPm );
    diffuseColor.rgb *= bisAlbedo( max( bisRad - ${LG_COAT_B_GLSL}, vec3( 0.0 ) ), 0.0 );
    vec3 bisT2 = min( bisRad / ${ALBEDO_MAX_GLSL}, vec3( 1.0 ) );
    #ifdef BIS_SRGB_ALPHA
      // Worker (spec alpha): a translucent piece's alpha already lets what lies beneath through — a fully tinted
      // clear share would darken it a second time, so it is partly untinted (TRANSLUCENT_CLEAR).
      bisT2 = mix( bisT2, vec3( 1.0 ), ${glslNum(TRANSLUCENT_CLEAR)} );
    #endif
    float bisGamma = 2.0 * mix( ${LG_DEEP[0].toFixed(2)}, ${LG_DEEP[1].toFixed(2)}, clamp( bisE / 0.6, 0.0, 1.0 ) );
    bisTransTint = pow( max( bisT2, vec3( 1e-4 ) ), vec3( bisGamma ) ) / max( diffuseColor.rgb, vec3( 1e-3 ) );
    // Clear-light: the clear rim lenses darker surroundings so the white frosted glyph separates from the pale plate.
    bisTransTint *= mix( 1.0 - bisLgEdgeDark, 1.0, smoothstep( 0.0, 0.6, bisE ) );
  #elif defined( BIS_ALBEDO )
    // Worker diffuse_paint(col, B) = _albedo(display_paint(col, DIFFUSE_PEAK), B): diffuse presets reach the displayed
    // paint under the calibrated rig.
    #ifdef BIS_SPEC_TINT
      // Worker b_satin: tint = rad / max(rad); albedo of (rad − tint · B) — the specular reflection is hue-tinted.
      vec3 bisSRad = bisDisplayPaint( bisPm, ${glslNum(DIFFUSE_PEAK)} );
      float bisSMx = max( max( bisSRad.r, bisSRad.g ), bisSRad.b );
      bisSpecTint = bisSRad / max( bisSMx, 1e-4 );
      #ifdef BIS_BRAND_PAINT
        // 'brand' has no toe offset: the face-on sheen (~DIFFUSE_B) was a floor no albedo could take off — dark paints
        // get a proportionally weaker specular and coat (worker: map_range(max(rad), 0, SATIN_DARK, 0.12, 1)).
        bisSatinCoat = mix( 0.12, 1.0, clamp( bisSMx / ${glslNum(SATIN_DARK)}, 0.0, 1.0 ) );
        bisSpecTint *= bisSatinCoat;
      #endif
      diffuseColor.rgb *= bisAlbedo( bisSRad - bisSpecTint * bisAlbedoB, 0.0 );
    #else
      diffuseColor.rgb *= bisAlbedo( bisDisplayPaint( bisPm, ${glslNum(DIFFUSE_PEAK)} ), bisAlbedoB ) * bisAlbedoGain;
    #endif
  #else
    diffuseColor.rgb *= bisPm;
    #if defined( BIS_PAINT_PERCEPTUAL ) && !defined( BIS_TRANSMIT_BODY )
      // (BIS_TRANSMIT_BODY, prism: a Glass BSDF tints both interfaces — the body colour once, bisTransTint stays 1.)
      // Glass presets (worker glass_colors: transmission colour sqrt(body)): measured in Cycles 5.0, a clear / frosted /
      // tinted glass glyph over a white plate shows ≈ sqrt(body) — the transmission tint acts once per slab, not
      // twice (#e04030, tint 1: Cycles (220, 124, 111), body² would be (247, 82, 70)) — so the transmitted light is
      // tinted sqrt(body) × GLASS_TRANSMIT (Cycles ≈ 0.81 · sqrt(body) + 0.02 over a white plate; three.js' own
      // transmission of the plate reads 1.07× + 0.05); the milky / diffuse share keeps the body colour.
      bisTransTint = ${glslNum(GLASS_TRANSMIT)} * sqrt( max( bisPm, vec3( 0.0 ) ) ) / max( bisPm, vec3( 1e-4 ) );
    #endif
  #endif
#endif
#ifdef BIS_PAINT_ALPHA
  diffuseColor.a *= texture2D( bisPaintMap, bisPaintCoord() ).a;
#endif
#ifdef BIS_SRGB_ALPHA
  // Worker _alpha: SVG composites opacity in gamma-encoded sRGB (a 33 % black overlay keeps 0.67 of the sRGB value,
  // 0.41 in linear light); the linear-light equivalent is 1 − (1 − a)^k, k = 2.2 − 1.5 · L (L = paint lightness).
  // Only the front face is drawn here, so the whole k applies (the worker splits it over the slab's two faces).
  diffuseColor.a = 1.0 - pow( clamp( 1.0 - diffuseColor.a, 0.0, 1.0 ), 2.2 - 1.5 * bisLightness( bisP ) );
#endif
#ifdef BIS_FILM
  // Worker _film: the body is a display-space blend film — no diffuse, no transmission (blend colour = T).
  diffuseColor.rgb = vec3( 0.0 );
#endif
#ifdef BIS_FAKE_GLASS
  #ifdef BIS_LG
    vec3 bisGlassColor = bisAlbedo( bisRad, 0.0 ); // worker: _fake_glass(_albedo(t2 · ALBEDO_MAX))
  #elif defined( BIS_BACKDROP_GLASS )
    vec3 bisGlassColor = diffuseColor.rgb; // worker _backdrop_glass: the wallpaper × the glass body colour (base_m)
  #else
    vec3 bisGlassColor = diffuseColor.rgb * bisTransTint;
  #endif
  #ifdef BIS_BACKDROP_GLASS
    float bisFakeT = 1.0; // black dielectric: everything seen is the (emitted) wallpaper below
  #else
    float bisFakeT = bisFakeTransmit * ( 1.0 - bisMilkV );
  #endif
  diffuseColor.rgb *= 1.0 - bisFakeT;
#endif
`

const EMISSIVE_ADD = /* glsl */ `
#include <emissivemap_fragment>
vec3 bisV = isOrthographic ? vec3( 0.0, 0.0, 1.0 ) : normalize( vViewPosition );
float bisNV = saturate( dot( normal, bisV ) );
#if BIS_PAINT_MODE == 2
  vec3 bisHot = mix( bisP, vec3( 1.0 ), bisCore * bisNV * bisNV );
  #ifdef BIS_DISPLAY_EMISSION
    bisHot = bisPaintRadiance( bisHot ); // worker b_flat: emits the displayed paint
  #endif
  totalEmissiveRadiance += bisHot * bisEmissive;
#else
  totalEmissiveRadiance += bisP * bisEmissive;
#endif
#ifdef BIS_LG
  // Liquid Glass (worker _rim_lg, glow, self-lit fill): the light direction projected into the icon plane.
  vec2 bisLxy = length( bisRimDir.xy ) > 1e-3 ? normalize( bisRimDir.xy ) : vec2( 0.0, 1.0 );
  float bisNl = length( bisN0.xy );
  float bisLs = bisNl > 1e-5 ? dot( bisN0.xy / bisNl, bisLxy ) : 0.0;
  float bisBackSide = max( -bisLs, 0.0 );
  // Dark paints: the light-locked rim is subdued and the inner glow carries no white (smoked glass).
  float bisRimL = mix( ${glslNum(LG_RIM_LIGHTNESS.to[0])}, ${glslNum(LG_RIM_LIGHTNESS.to[1])}, smoothstep( ${glslNum(LG_RIM_LIGHTNESS.from[0])}, ${glslNum(LG_RIM_LIGHTNESS.from[1])}, bisLum ) );
  totalEmissiveRadiance += bisRimColor * ( pow( max( bisLs, 0.0 ), 2.0 ) + 0.16 * bisBackSide * bisBackSide ) * bisBand( bisLgBand, bisE ) * bisLgRim * bisRimL * bisNb * bisKeep;
  // Inner glow: saturated paints glow in their own colour (white share 0.3 · L · (1 − HSV saturation of the body)).
  float bisPmMx = max( max( bisPm.r, bisPm.g ), bisPm.b );
  float bisPmSat = bisPmMx > 1e-6 ? ( bisPmMx - min( min( bisPm.r, bisPm.g ), bisPm.b ) ) / bisPmMx : 0.0;
  totalEmissiveRadiance += mix( bisPm, vec3( 1.0 ), 0.3 * bisLum * ( 1.0 - bisPmSat ) ) * bisBand( vec4( 0.3, 0.45, 0.8, 0.95 ), bisE ) * ( 0.08 + 0.92 * bisBackSide ) * bisLgGlow * bisKeep;
  #ifdef BIS_FILM
    // Film emission E, less the share of the (alpha-weighted) coat's face-on studio reflection (worker _film).
    totalEmissiveRadiance += max( bisFilmE - bisTame * bisCoatNb * bisFilmAlpha * ${LG_COAT_B_GLSL}, vec3( 0.0 ) );
  #else
    totalEmissiveRadiance += max( bisRad - ${LG_COAT_B_GLSL}, vec3( 0.0 ) ) * bisFill * ${(1 - LG_FILL_DIFFUSE).toFixed(4)} * mix( 0.95, 1.04, bisV01 ) * bisLgLit * ( 1.0 + 0.45 * bisLgEdgeDark );
  #endif
#else
  // Light-angle-locked rim (glass doc §3.2): (max(N·L,0)^3 + 0.45·max(−N·L,0)^3) × Fresnel
  vec3 bisNW = inverseTransformDirection( normal, viewMatrix );
  float bisD = dot( bisNW, bisRimDir );
  float bisFres = pow( 1.0 - bisNV, 2.0 );
  float bisRim = ( pow( max( bisD, 0.0 ), 3.0 ) * bisRimKey + pow( max( -bisD, 0.0 ), 3.0 ) * bisRimBack ) * bisFres;
  totalEmissiveRadiance += bisRimColor * bisRim * 6.0;
  totalEmissiveRadiance += bisP * bisGlow * ( 1.0 - bisNV );
#endif
#ifdef BIS_FAKE_GLASS
  #if BIS_BEHIND_SPACE == 0
    vec2 bisBuv = vBisWorld.xy * bisBehindScale + 0.5;
  #else
    vec2 bisBuv = ( vBisClip.xy / vBisClip.w ) * 0.5 + 0.5;
  #endif
  bisBuv -= normal.xy * bisFakeRefract * ( 1.0 - bisNV );
  #ifdef BIS_BEHIND_MAP
    vec3 bisBehind = texture2D( bisBehindMap, bisBuv, bisBehindLod ).rgb;
  #else
    vec3 bisBehind = bisBehindColor;
  #endif
  #ifdef BIS_BACKDROP_GLASS
    // Worker _backdrop_glass: wallpaper × glass colour, greyed on the dark wallpaper, screened with the frost scatter.
    vec3 bisSeen = bisBehind * bisGlassColor;
    bisSeen = mix( bisSeen, vec3( dot( bisSeen, vec3( 0.2126, 0.7152, 0.0722 ) ) ), bisBdDesat );
    totalEmissiveRadiance += 1.0 - ( 1.0 - clamp( bisSeen, 0.0, 1.0 ) ) * ( 1.0 - bisBdScatter );
  #else
    float bisF = 0.04 + 0.96 * pow( 1.0 - bisNV, 5.0 );
    totalEmissiveRadiance += bisBehind * bisGlassColor * bisFakeT * ( 1.0 - bisF ) * 0.9;
  #endif
#endif
`

// Liquid Glass: specular level and coat fade out toward the silhouette (worker: e 0.08 → 0.6), tamed grazing
// reflections; the self-lit fill's diffuse share is LG_FILL_DIFFUSE (the rest is emission, added above).
const LG_LIGHTS = /* glsl */ `
#include <lights_physical_fragment>
#ifdef BIS_SPEC_TINT
  material.specularColor *= bisSpecTint;
  material.specularColorBlended *= bisSpecTint;
  #ifdef USE_CLEARCOAT
    material.clearcoat *= bisSatinCoat;
  #endif
#endif
#ifdef BIS_LG
  // Dark smoked glass: the grazing coat / specular sheen of the bright studio world would outline every dark piece in
  // white — subdued by dk = 0.35 → 1 over L 0 → 0.5 (worker).
  // (bisTame: see PAINT_APPLY — × keep along a flush edge)
  #ifdef BIS_FILM
    // A film carries only its coat (its share: × alpha) — worker _coat_only: no dielectric specular.
    material.specularColor = vec3( 0.0 );
    material.specularColorBlended = vec3( 0.0 );
    material.specularF90 = 0.0;
  #else
    material.specularColor *= bisTame;
    material.specularColorBlended *= bisTame;
    material.specularF90 *= bisTame;
  #endif
  #ifdef USE_CLEARCOAT
    material.clearcoat *= bisTame * bisCoatNb;
    #ifdef BIS_FILM
      material.clearcoat *= bisFilmAlpha;
    #endif
  #endif
#endif
`

const LG_AO = /* glsl */ `
#ifdef BIS_LG
  reflectedLight.directDiffuse *= ${LG_FILL_DIFFUSE.toFixed(4)};
  reflectedLight.indirectDiffuse *= ${LG_FILL_DIFFUSE.toFixed(4)};
#endif
#include <aomap_fragment>
`

const warned = new Set<string>()
/** String injection into three's shader source; warns (once) if a three.js upgrade moved the anchor. */
function inject(src: string, anchor: string, replacement: string): string {
  if (!src.includes(anchor)) {
    if (!warned.has(anchor)) {
      warned.add(anchor)
      console.warn(`[materials3d] shader anchor not found (three.js changed?): ${anchor}`)
    }
    return src
  }
  return src.replace(anchor, replacement)
}

function patchShader(shader: THREE.WebGLProgramParametersWithUniforms, uniforms: IconUniforms): void {
  Object.assign(shader.uniforms, uniforms)
  let vs = inject(shader.vertexShader, 'void main() {', `${VERTEX_PARS}\nvoid main() {`)
  vs = inject(vs, '#include <project_vertex>', VERTEX_MAIN)
  shader.vertexShader = vs

  const normalBegin = inject(
    THREE.ShaderChunk.normal_fragment_begin,
    'vec3 nonPerturbedNormal = normal;',
    '#ifdef BIS_INFLATE\n\tnormal = normalize( normal + vBisTilt );\n#endif\nvec3 nonPerturbedNormal = normal;',
  )
  let transmission = inject(
    THREE.ShaderChunk.transmission_fragment,
    'material.transmission = transmission;',
    'material.transmission = transmission * ( 1.0 - bisMilkV );',
  )
  transmission = inject(
    transmission,
    'material.roughness, material.diffuseContribution, material.specularColorBlended',
    'material.roughness, material.diffuseContribution * bisTransTint, material.specularColorBlended',
  )
  transmission = inject(
    transmission,
    'material.attenuationColor = attenuationColor;',
    'material.attenuationColor = mix( attenuationColor, clamp( bisP, vec3( 0.002 ), vec3( 1.0 ) ), bisAttenuationMix );',
  )
  let fs = inject(shader.fragmentShader, 'void main() {', `${FRAGMENT_PARS}\nvoid main() {`)
  fs = inject(fs, '#include <map_fragment>', PAINT_APPLY)
  fs = inject(fs, '#include <normal_fragment_begin>', normalBegin)
  fs = inject(fs, '#include <emissivemap_fragment>', EMISSIVE_ADD)
  fs = inject(fs, '#include <transmission_fragment>', transmission)
  fs = inject(fs, '#include <lights_physical_fragment>', LG_LIGHTS)
  fs = inject(fs, '#include <aomap_fragment>', LG_AO)
  shader.fragmentShader = fs
}

/** MeshPhysicalMaterial with the Blender-mirroring paint / rim / fake-glass patch. One per mesh. */
export class IconMaterial extends THREE.MeshPhysicalMaterial {
  readonly bis: IconUniforms = createUniforms()
  /** Last applied program topology (defines + map presence). */
  topology = ''
  /** Whether the last applied spec rendered as fake glass. */
  isFakeGlass = false
  /** Whether the last applied spec is a display-space blend film (custom blending set by applyIconMaterial). */
  isFilm = false

  constructor() {
    super({ dithering: true })
    this.onBeforeCompile = (shader) => patchShader(shader, this.bis)
  }

  override customProgramCacheKey(): string {
    return 'bis-icon-v5'
  }
}

const filmTextures = new Map<number, THREE.Texture>()
function filmTexture(bands: number): THREE.Texture {
  const key = Math.round(clamp(bands, 0.2, 12) * 10) / 10
  let t = filmTextures.get(key)
  if (!t) {
    t = getFilmNoiseTexture().clone()
    t.repeat.set(key / 3, key / 3)
    t.needsUpdate = true
    filmTextures.set(key, t)
  }
  return t
}

const PAINT_MODE_INDEX: Record<PaintMode, number> = { tint: 0, base: 1, emission: 2 }
const INTENT_INDEX = { color: 0, mono: 1, tint: 2 } as const
const _c = new THREE.Color()

/** Push `spec` + `ctx` into `m` (in place). Triggers a program switch only when the shader topology changes. */
export function applyIconMaterial(m: IconMaterial, s: IconMaterialSpec, ctx: MaterialContext): void {
  const u = m.bis
  const paintAlpha = !!ctx.paintAlpha && !!ctx.paint.map
  const overlay = paintAlpha && (ctx.paint.alphaMax ?? 1) < OVERLAY_ALPHA_MAX
  const refract = s.transmission > 0 && !overlay
  const fake = ctx.fake && refract ? ctx.fake : null
  m.isFakeGlass = !!fake

  const defines: Record<string, string | number> = {
    // MeshPhysicalMaterial's own defines (they gate IOR, clearcoat, transmission… in the shader).
    STANDARD: '',
    PHYSICAL: '',
    BIS_PAINT_MODE: PAINT_MODE_INDEX[s.paintMode],
    BIS_INTENT: INTENT_INDEX[s.intent.intent],
  }
  if (ctx.paint.map) defines.BIS_PAINT_MAP = ''
  if (ctx.paint.monoLut && s.intent.intent !== 'color') defines.BIS_MONO_LUT = ''
  if (paintAlpha) defines.BIS_PAINT_ALPHA = ''
  // Translucent bodies composite like the SVG (sRGB-space opacity, see BIS_SRGB_ALPHA); a film blends by itself.
  if (paintAlpha || (ctx.opacity < 0.999 && !(s.lg && ctx.film))) defines.BIS_SRGB_ALPHA = ''
  if (fake) {
    defines.BIS_FAKE_GLASS = ''
    defines.BIS_BEHIND_SPACE = fake.space === 'canvas' ? 0 : 1
    if (fake.map) defines.BIS_BEHIND_MAP = ''
    if (fake.backdropGlass && fake.tone) defines.BIS_BACKDROP_GLASS = ''
  }
  const inflate = ctx.inflate ?? 0
  if (inflate > 1e-4) defines.BIS_INFLATE = ''
  if (s.milkByLum) defines.BIS_MILK_LUM = ''
  if (s.whiteMilk > 0 && !s.lg && !ctx.plate) defines.BIS_WHITE_MILK = ''
  const film = s.lg && ctx.film ? ctx.film : null
  m.isFilm = !!film
  if (film) defines.BIS_FILM = ''
  if (s.lg && ctx.flush) defines.BIS_FLUSH = ''
  if (s.paintPerceptual) defines.BIS_PAINT_PERCEPTUAL = ''
  if (s.paintPerceptual && s.transmitBody) defines.BIS_TRANSMIT_BODY = ''
  if (s.lg) defines.BIS_LG = ''
  const paintTransform = ctx.displayPaint ? asTransform(ctx.displayPaint) : 'identity'
  if (paintTransform === 'neutral') defines.BIS_DISPLAY_PAINT = ''
  else if (paintTransform === 'brand') defines.BIS_BRAND_PAINT = ''
  if (s.albedoB != null && !s.lg) defines.BIS_ALBEDO = ''
  if (s.displayEmission) defines.BIS_DISPLAY_EMISSION = ''
  if (s.specularTint && s.albedoB != null && !s.lg) defines.BIS_SPEC_TINT = ''

  // ---------------------------------------------------------------- physical properties
  m.color.setRGB(s.baseColor[0], s.baseColor[1], s.baseColor[2])
  m.roughness = clamp(s.roughness, 0, 1)
  m.metalness = clamp(s.metalness, 0, 1)
  m.transmission = fake || !refract || film ? 0 : clamp(s.transmission, 0, 1)
  m.thickness = Math.max(0, ctx.thickness * s.thicknessScale)
  m.ior = clamp(s.ior, 1, 2.333)
  m.clearcoat = clamp(s.clearcoat, 0, 1)
  m.clearcoatRoughness = clamp(s.clearcoatRoughness, 0, 1)
  m.specularIntensity = s.specularIntensity
  m.envMapIntensity = s.envMapIntensity
  m.sheen = clamp(s.sheen, 0, 1)
  m.sheenRoughness = clamp(s.sheenRoughness, 0, 1)
  _c.setRGB(1, 1, 1).lerp(ctx.paint.color, clamp(s.sheenTint, 0, 1))
  m.sheenColor.copy(_c)
  m.iridescence = clamp(s.iridescence, 0, 1)
  m.iridescenceIOR = s.iridescenceIOR
  m.iridescenceThicknessRange = [s.iridescenceRange[0], s.iridescenceRange[1]]
  m.iridescenceThicknessMap = s.iridescence > 0 ? filmTexture(s.iridescenceBands) : null
  m.dispersion = fake || !refract || film ? 0 : Math.max(0, s.dispersion)
  m.anisotropy = clamp(s.anisotropy, 0, 1)
  m.anisotropyMap = s.anisotropy > 0 && s.anisotropyRadial ? getRadialAnisotropyMap() : null
  if (s.grain > 0) {
    const g = getGrainNormalMap()
    if (g.repeat.x !== 28) {
      g.repeat.set(28, 28)
      g.needsUpdate = true
    }
    m.normalMap = g
    m.normalScale.setScalar(s.grain * 2.5)
  } else {
    m.normalMap = null
  }
  m.attenuationColor.setRGB(1, 1, 1)
  m.attenuationDistance =
    s.attenuationMix > 0 && Number.isFinite(s.attenuationDistanceScale)
      ? Math.max(1e-3, m.thickness * (ctx.modelScale ?? 1) * s.attenuationDistanceScale)
      : Infinity
  m.opacity = film ? 1 : clamp(ctx.opacity, 0, 1)
  m.transparent = film ? true : ctx.opacity < 0.999 || paintAlpha
  m.depthWrite = true

  // ---------------------------------------------------------------- patch uniforms
  u.bisPaintMap.value = ctx.paint.map
  const puv = ctx.paint.uv ?? ART_SQUARE_UV
  u.bisPaintUv.value.set(puv[0], puv[1], puv[2], puv[3], puv[4], puv[5], 0, 0, 1)
  u.bisPaintColor.value.copy(ctx.paint.color)
  u.bisPaintMix.value = clamp(s.paintMix, 0, 1)
  u.bisNeutral.value.setRGB(s.neutral[0], s.neutral[1], s.neutral[2])
  // PaintBinding.lumRange is linear luminance; the mono stretch works on perceptual luminance (^1/2.2).
  u.bisLumRange.value.set(
    Math.max(0, ctx.paint.lumRange[0]) ** (1 / 2.2),
    Math.max(0, ctx.paint.lumRange[1]) ** (1 / 2.2),
  )
  u.bisMonoLut.value = ctx.paint.monoLut ?? null
  u.bisIntentTint.value.setStyle(s.intent.tintColor)
  u.bisIntentStrength.value = clamp(s.intent.tintStrength, 0, 1)
  u.bisEmissive.value = s.emissive
  u.bisCore.value = s.core
  u.bisAttenuationMix.value = s.attenuationMix
  u.bisRimDir.value.copy(ctx.rimDir).normalize()
  u.bisRimColor.value.copy(ctx.rimColor ?? _c.setRGB(1, 1, 1))
  u.bisRimKey.value = s.rim.key
  u.bisRimBack.value = s.rim.back
  u.bisGlow.value = s.rim.glow
  u.bisBehindMap.value = fake?.map ?? null
  u.bisBehindColor.value.copy(fake?.color ?? _c.setRGB(1, 1, 1))
  u.bisBehindLod.value = fake ? 1 + s.roughness * 10 : 0
  u.bisBehindScale.value = 0.5 / Math.max(1e-3, fake?.extent ?? 1)
  u.bisFakeTransmit.value = fake ? clamp(s.transmission, 0, 1) * 0.92 : 0
  u.bisFakeRefract.value = fake ? 0.035 * (s.ior - 1) : 0
  const darkWall = fake?.tone === 'dark'
  // dark: the veil is mostly the pane's reflection of the studio — it follows the coat (worker _backdrop_glass)
  u.bisBdScatter.value = clamp((0.1 + 0.35 * s.roughness) * (darkWall ? 0.32 * Math.min(1, s.clearcoat) : 1), 0, 1)
  u.bisBdDesat.value = darkWall ? 0.45 : 0
  u.bisCenter.value.set(ctx.center?.[0] ?? 0, ctx.center?.[1] ?? 0)
  u.bisRadius.value = ctx.radius ?? 1
  u.bisInflate.value = inflate
  u.bisMilk.value = Math.max(0, s.milk)
  u.bisAlbedoB.value = s.albedoB ?? 0
  u.bisAlbedoGain.value = s.albedoGain
  u.bisWhiteMilk.value = s.whiteMilk
  u.bisMilkRange.value.set(ctx.milkRange?.[0] ?? -1, ctx.milkRange?.[1] ?? 1)
  if (s.lg) {
    const lit = ctx.lit ?? 1
    u.bisLgFill.value.set(s.lg.fill[0], s.lg.fill[1])
    u.bisLgRim.value = s.lg.rim * (ctx.rimScale ?? 1)
    u.bisLgSolidEdge.value = clamp(ctx.solidEdge ?? 0, 0, 1)
    const fl = ctx.flush
    if (fl) {
      const r = fl.shape === 'circle' ? 1 : fl.shape === 'square' ? 0 : fl.r
      u.bisFlush.value.set(fl.grow, r, fl.shape === 'squircle' ? 1 : 0, 0)
      u.bisFlushBand.value.set(fl.band[0], fl.band[1])
    }
    u.bisFilmE.value.set(film?.e[0] ?? 0, film?.e[1] ?? 0, film?.e[2] ?? 0)
    u.bisFilmAlpha.value = film ? clamp(ctx.opacity, 0, 1) : 1
    u.bisLgBand.value.set(s.lg.band[0], s.lg.band[1], s.lg.band[2], s.lg.band[3])
    u.bisLgGlow.value = 1.4 * s.lg.glow * lit
    u.bisLgLit.value = lit
    u.bisLgEdgeDark.value = s.lg.edgeDark
  }

  const topology = [
    JSON.stringify(defines),
    m.normalMap ? 'n' : '',
    m.anisotropyMap ? 'a' : '',
    m.iridescenceThicknessMap ? 'i' : '',
    m.transparent ? 't' : '',
  ].join('|')
  // Film: dst' = src + T · dst (radiance), alpha kept (blend colour = T, worker _film's per-piece transmission).
  if (film) {
    m.blending = THREE.CustomBlending
    m.blendEquation = THREE.AddEquation
    m.blendSrc = THREE.OneFactor
    m.blendDst = THREE.ConstantColorFactor
    m.blendColor.setRGB(clamp(film.t[0], 0, 1), clamp(film.t[1], 0, 1), clamp(film.t[2], 0, 1))
    m.blendEquationAlpha = THREE.AddEquation
    m.blendSrcAlpha = THREE.ZeroFactor
    m.blendDstAlpha = THREE.OneFactor
  }
  if (topology !== m.topology) {
    m.defines = defines
    m.topology = topology
    m.needsUpdate = true
  }
}

/**
 * three.js draws `transparent` materials after the transmissive (glass) ones and leaves them out of the transmission
 * buffer, so a blended body — layer opacity < 1, soft raster alpha, a blend mode — lying under a glass layer is
 * depth-occluded by that glass and never refracted by it: it simply vanishes (Blender shows it through the glass).
 * Blended, non-refractive bodies are therefore drawn in the opaque pass instead: `transparent = false` with an explicit
 * blend function (three.js only drops blending for NormalBlending on opaque materials), after the opaque bodies and
 * back to front through `renderOrder` (see BLENDED_RENDER_ORDER). Returns whether the material was routed.
 * Callers route only bodies that are not lying on top of refracting glass (LayerStack StackEntry.routeBlended): drawn
 * before that glass, a routed body would occlude it (depth) and be refracted by it instead of covering it.
 */
export function blendInOpaquePass(m: THREE.MeshPhysicalMaterial): boolean {
  if (!m.transparent || m.transmission > 0) return false
  if (m.blending === THREE.NormalBlending) {
    m.blending = THREE.CustomBlending
    m.blendEquation = THREE.AddEquation
    m.blendSrc = m.premultipliedAlpha ? THREE.OneFactor : THREE.SrcAlphaFactor
    m.blendDst = THREE.OneMinusSrcAlphaFactor
    m.blendEquationAlpha = THREE.AddEquation
    m.blendSrcAlpha = THREE.OneFactor
    m.blendDstAlpha = THREE.OneMinusSrcAlphaFactor
  }
  m.transparent = false
  return true
}

/** renderOrder base of blended bodies in the opaque pass: plate 1, layer at stack level L → 2 + L (+ sub-order). */
export const BLENDED_RENDER_ORDER = { plate: 1, layer: 2 } as const

/** Convenience: true when the preset renders with real transmission (glass). */
export function isTransmissive(s: IconMaterialSpec): boolean {
  return s.transmission > 0
}

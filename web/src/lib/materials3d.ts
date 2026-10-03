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
// applyIconMaterial() pushes a spec + context into an IconMaterial in place (recompiles only on topology change).
import * as THREE from 'three'
import type { MaterialSpec, Presets } from '../types'
import { readMaterialIntent, type MaterialIntent } from './appearance'
import { getFilmNoiseTexture, getGrainNormalMap, getRadialAnisotropyMap } from '../viewport/textures/procedural'

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
  /** Neon hot core: mix toward white where the tube faces the camera. */
  core: number
  rim: { key: number; back: number; glow: number }
  /** Requested compositor bloom (neon). */
  bloom: number
  unlit: boolean
  intent: MaterialIntent
}

const WHITE: RGB = [1, 1, 1]

/** Worker `tint_curve`: UI tint (0..1) → colour mix factor, eased out so the default 0.45 keeps colours vivid. */
export function tintCurve(t: number): number {
  const c = Math.max(0, Math.min(1, t))
  return 1 - (1 - c) ** 4
}

/**
 * Mono / tint renditions (PLAN §5, worker `_mono`): perceptual luminance stretched from the icon-wide range to
 * MONO_FLOOR..1. A narrow range is never over-stretched (lo ≤ hi − MONO_MIN_RANGE), like the worker.
 */
export const MONO_FLOOR = 0.25
export const MONO_MIN_RANGE = 0.55

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
    core: 0,
    rim: { key: 0, back: 0, glow: 0 },
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
      const rim = num('rim', 0.8)
      glassTint(0.45)
      s.transmission = 1
      s.milk = (1 - clamp(num('translucency', 0.6), 0, 1)) * 1.6
      s.roughness = num('frost', 0.12)
      s.ior = num('ior', 1.45)
      s.thicknessScale = 2.8
      s.clearcoat = specular === 'off' ? 0 : 1
      s.clearcoatRoughness = 0.02
      s.specularIntensity = specular === 'off' ? 0.45 : 1
      s.rim = {
        key: specular === 'off' ? 0 : specular === 'inside' ? 0.3 * rim : rim,
        back: specular === 'off' || specular === 'outside' ? 0 : 0.45 * rim * (specular === 'inside' ? 2.2 : 1),
        glow: num('glow', 0),
      }
      break
    }
    case 'clear_glass':
      glassTint(0.1)
      s.transmission = 1
      s.roughness = num('frost', 0)
      s.ior = num('ior', 1.5)
      s.thicknessScale = 2.4
      s.rim = { key: 0.25, back: 0.1, glow: 0 }
      break
    case 'frosted_glass':
      glassTint(0.25)
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
      s.roughness = num('roughness', 0.35)
      s.clearcoat = num('coat', 1)
      s.clearcoatRoughness = num('coatRoughness', 0.03)
      s.rim = { key: 0.07, back: 0.03, glow: 0 }
      break
    case 'satin':
      s.roughness = num('roughness', 0.45)
      s.clearcoat = num('coat', 0.25)
      s.clearcoatRoughness = 0.06
      s.specularIntensity = 0.9
      s.sheen = 0.08
      s.sheenRoughness = 0.5
      break
    case 'candy': {
      const sss = num('subsurface', 1)
      const trans = num('transmission', 0)
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
}

export interface FakeGlassBinding {
  /** What the glass shows through itself: the plate fill / wallpaper (canvas space) or the backdrop (screen space). */
  map: THREE.Texture | null
  color: THREE.Color
  space: 'canvas' | 'screen'
  /** Canvas space only: the map covers world x, y ∈ ±extent (default 1 = the plate square). */
  extent?: number
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
}

function createUniforms() {
  return {
    bisPaintMap: { value: null as THREE.Texture | null },
    bisPaintUv: { value: new THREE.Matrix3().set(0.5, 0, 0.5, 0, 0.5, 0.5, 0, 0, 1) },
    bisPaintColor: { value: new THREE.Color(1, 1, 1) },
    bisPaintMix: { value: 1 },
    bisNeutral: { value: new THREE.Color(1, 1, 1) },
    bisLumRange: { value: new THREE.Vector2(0, 1) },
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
    bisMilk: { value: 0 },
    bisMilkRange: { value: new THREE.Vector2(-1, 1) },
    bisCenter: { value: new THREE.Vector2() },
    bisRadius: { value: 1 },
    bisInflate: { value: 0 },
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
#endif
#ifdef BIS_FAKE_GLASS
  uniform vec3 bisBehindColor;
  uniform float bisBehindLod;
  uniform float bisBehindScale;
  uniform float bisFakeTransmit;
  uniform float bisFakeRefract;
  #ifdef BIS_BEHIND_MAP
    uniform sampler2D bisBehindMap;
  #endif
#endif
#ifdef BIS_INFLATE
  varying vec3 vBisTilt;
#endif

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
    float hi = bisLumRange.y;
    float lo = min( bisLumRange.x, hi - ${MONO_MIN_RANGE.toFixed(4)} );
    float st = mix( ${MONO_FLOOR.toFixed(4)}, 1.0, clamp( ( l - lo ) / max( hi - lo, 1e-4 ), 0.0, 1.0 ) );
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
  #endif
  return clamp( m, 0.0, 1.0 );
}
`

const PAINT_APPLY = /* glsl */ `
#include <map_fragment>
vec3 bisP = bisPaintSample();
float bisMilkV = bisMilkAmount( bisP );
#if BIS_PAINT_MODE != 2
  #ifdef BIS_PAINT_PERCEPTUAL
    // Worker tinted(): mix(white, paint, tintCurve(tint)) in gamma-2.2 space.
    diffuseColor.rgb *= pow( mix( pow( bisNeutral, vec3( 1.0 / 2.2 ) ), pow( max( bisP, vec3( 0.0 ) ), vec3( 1.0 / 2.2 ) ), bisPaintMix ), vec3( 2.2 ) );
  #else
    diffuseColor.rgb *= mix( bisNeutral, bisP, bisPaintMix );
  #endif
#endif
#ifdef BIS_PAINT_ALPHA
  diffuseColor.a *= texture2D( bisPaintMap, bisPaintCoord() ).a;
#endif
#ifdef BIS_FAKE_GLASS
  vec3 bisGlassColor = diffuseColor.rgb;
  float bisFakeT = bisFakeTransmit * ( 1.0 - bisMilkV );
  diffuseColor.rgb *= 1.0 - bisFakeT;
#endif
`

const EMISSIVE_ADD = /* glsl */ `
#include <emissivemap_fragment>
vec3 bisV = isOrthographic ? vec3( 0.0, 0.0, 1.0 ) : normalize( vViewPosition );
float bisNV = saturate( dot( normal, bisV ) );
#if BIS_PAINT_MODE == 2
  vec3 bisHot = mix( bisP, vec3( 1.0 ), bisCore * bisNV * bisNV );
  totalEmissiveRadiance += bisHot * bisEmissive;
#else
  totalEmissiveRadiance += bisP * bisEmissive;
#endif
// Light-angle-locked rim (glass doc §3.2): (max(N·L,0)^3 + 0.45·max(−N·L,0)^3) × Fresnel
vec3 bisNW = inverseTransformDirection( normal, viewMatrix );
float bisD = dot( bisNW, bisRimDir );
float bisFres = pow( 1.0 - bisNV, 2.0 );
float bisRim = ( pow( max( bisD, 0.0 ), 3.0 ) * bisRimKey + pow( max( -bisD, 0.0 ), 3.0 ) * bisRimBack ) * bisFres;
totalEmissiveRadiance += bisRimColor * bisRim * 6.0;
totalEmissiveRadiance += bisP * bisGlow * ( 1.0 - bisNV );
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
  float bisF = 0.04 + 0.96 * pow( 1.0 - bisNV, 5.0 );
  totalEmissiveRadiance += bisBehind * bisGlassColor * bisFakeT * ( 1.0 - bisF ) * 0.9;
#endif
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
    'material.attenuationColor = attenuationColor;',
    'material.attenuationColor = mix( attenuationColor, clamp( bisP, vec3( 0.002 ), vec3( 1.0 ) ), bisAttenuationMix );',
  )
  let fs = inject(shader.fragmentShader, 'void main() {', `${FRAGMENT_PARS}\nvoid main() {`)
  fs = inject(fs, '#include <map_fragment>', PAINT_APPLY)
  fs = inject(fs, '#include <normal_fragment_begin>', normalBegin)
  fs = inject(fs, '#include <emissivemap_fragment>', EMISSIVE_ADD)
  fs = inject(fs, '#include <transmission_fragment>', transmission)
  shader.fragmentShader = fs
}

/** MeshPhysicalMaterial with the Blender-mirroring paint / rim / fake-glass patch. One per mesh. */
export class IconMaterial extends THREE.MeshPhysicalMaterial {
  readonly bis: IconUniforms = createUniforms()
  /** Last applied program topology (defines + map presence). */
  topology = ''
  /** Whether the last applied spec rendered as fake glass. */
  isFakeGlass = false

  constructor() {
    super({ dithering: true })
    this.onBeforeCompile = (shader) => patchShader(shader, this.bis)
  }

  override customProgramCacheKey(): string {
    return 'bis-icon-v2'
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
  const fake = ctx.fake && s.transmission > 0 ? ctx.fake : null
  m.isFakeGlass = !!fake

  const defines: Record<string, string | number> = {
    // MeshPhysicalMaterial's own defines (they gate IOR, clearcoat, transmission… in the shader).
    STANDARD: '',
    PHYSICAL: '',
    BIS_PAINT_MODE: PAINT_MODE_INDEX[s.paintMode],
    BIS_INTENT: INTENT_INDEX[s.intent.intent],
  }
  const paintAlpha = !!ctx.paintAlpha && !!ctx.paint.map
  if (ctx.paint.map) defines.BIS_PAINT_MAP = ''
  if (paintAlpha) defines.BIS_PAINT_ALPHA = ''
  if (fake) {
    defines.BIS_FAKE_GLASS = ''
    defines.BIS_BEHIND_SPACE = fake.space === 'canvas' ? 0 : 1
    if (fake.map) defines.BIS_BEHIND_MAP = ''
  }
  const inflate = ctx.inflate ?? 0
  if (inflate > 1e-4) defines.BIS_INFLATE = ''
  if (s.milkByLum) defines.BIS_MILK_LUM = ''
  if (s.paintPerceptual) defines.BIS_PAINT_PERCEPTUAL = ''

  // ---------------------------------------------------------------- physical properties
  m.color.setRGB(s.baseColor[0], s.baseColor[1], s.baseColor[2])
  m.roughness = clamp(s.roughness, 0, 1)
  m.metalness = clamp(s.metalness, 0, 1)
  m.transmission = fake ? 0 : clamp(s.transmission, 0, 1)
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
  m.dispersion = fake ? 0 : Math.max(0, s.dispersion)
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
  m.opacity = clamp(ctx.opacity, 0, 1)
  m.transparent = ctx.opacity < 0.999 || paintAlpha
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
  u.bisCenter.value.set(ctx.center?.[0] ?? 0, ctx.center?.[1] ?? 0)
  u.bisRadius.value = ctx.radius ?? 1
  u.bisInflate.value = inflate
  u.bisMilk.value = Math.max(0, s.milk)
  u.bisMilkRange.value.set(ctx.milkRange?.[0] ?? -1, ctx.milkRange?.[1] ?? 1)

  const topology = [
    JSON.stringify(defines),
    m.normalMap ? 'n' : '',
    m.anisotropyMap ? 'a' : '',
    m.iridescenceThicknessMap ? 'i' : '',
    m.transparent ? 't' : '',
  ].join('|')
  if (topology !== m.topology) {
    m.defines = defines
    m.topology = topology
    m.needsUpdate = true
  }
}

/** Convenience: true when the preset renders with real transmission (glass). */
export function isTransmissive(s: IconMaterialSpec): boolean {
  return s.transmission > 0
}

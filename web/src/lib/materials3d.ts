// Shape materials of the live view (PLAN §11): every shape is ONE Principled BSDF (the 28-param schema of
// shared/presets.json), mirrored here as ONE THREE.MeshPhysicalMaterial per shape, a direct, input-by-input mapping of
// blender_worker/materials.py's graph:
//
//   Base Color      = Mix(white → art, tint)           paintMode 'emission': Mix(black → art, tint)
//   Emission        = art (paintMode emission / base+emission, else white) × emissionStrength
//   Metallic / Roughness / IOR / Transmission (+ the body thickness) / Alpha (× piece opacity × art alpha)
//   transmitted light takes Base Color (√ per interface in Cycles), once more when the body lies on what it shows
//   Specular IOR Level → specularIntensity (×2: 0.5 = no adjustment)   Specular Tint → specularColor (art mix)
//   Coat Weight / Roughness → clearcoat                                Sheen Weight / Roughness / Tint → sheen
//   Thin Film Thickness / IOR (+ filmVariation noise) → iridescence    Anisotropic (+ radial tangent) → anisotropy
//   grain (Noise → Bump) → a tiling micro-normal map
//   tinted renditions: art → luminance stretched to floor..1 × tint colour (worker env `mono`)
//
// No glow cards, overlays, milk / ice bodies or colour pre-compensation: three.js does the shading, Cycles the truth.
// Inputs three.js has no counterpart for (Subsurface, Diffuse Roughness, Coat IOR / Tint) are read but not drawn.
// The only rendering workaround is for glass seen through other glass (three.js, like EEVEE, has no transmission
// through transmission): such a covered glass body is drawn opaque, its transmitted share coloured by its base × what
// lies behind it (the rest stays the lit base).
import * as THREE from 'three'
import type { Layer, MaterialSpec, Presets } from '../types'
import { getFilmNoiseTexture, getGrainNormalMap, getRadialAnisotropyMap } from '../viewport/textures/procedural'

export type PaintMode = 'base' | 'emission' | 'base+emission'
export const PAINT_MODES: readonly PaintMode[] = ['base', 'emission', 'base+emission']

/** The Principled schema (shared/presets.json `materials.*.params`, PLAN §11). */
export interface Principled {
  paintMode: PaintMode
  tint: number
  grain: number
  grainScale: number
  filmVariation: number
  metallic: number
  roughness: number
  ior: number
  alpha: number
  diffuseRoughness: number
  subsurfaceWeight: number
  subsurfaceScale: number
  subsurfaceAnisotropy: number
  specularIorLevel: number
  specularTint: number
  anisotropic: number
  anisotropicRotation: number
  transmission: number
  coatWeight: number
  coatRoughness: number
  coatIor: number
  coatTint: number
  sheenWeight: number
  sheenRoughness: number
  sheenTint: number
  emissionStrength: number
  thinFilmThickness: number
  thinFilmIor: number
}

/** Liquid Glass defaults: used when presets.json is not available (harness without a backend). */
export const DEFAULT_PRINCIPLED: Principled = {
  paintMode: 'base',
  tint: 0.8,
  grain: 0,
  grainScale: 400,
  filmVariation: 0,
  metallic: 0,
  roughness: 0.05,
  ior: 1.5,
  alpha: 1,
  diffuseRoughness: 0,
  subsurfaceWeight: 0,
  subsurfaceScale: 0.05,
  subsurfaceAnisotropy: 0,
  specularIorLevel: 0.5,
  specularTint: 0,
  anisotropic: 0,
  anisotropicRotation: 0,
  transmission: 1,
  coatWeight: 0.5,
  coatRoughness: 0.02,
  coatIor: 1.5,
  coatTint: 0,
  sheenWeight: 0,
  sheenRoughness: 0.5,
  sheenTint: 0,
  emissionStrength: 0,
  thinFilmThickness: 0,
  thinFilmIor: 1.33,
}

/** Param names of projects / looks saved before the single-Principled schema (worker presets.LEGACY_PARAMS). */
export const LEGACY_PARAMS: Record<string, keyof Principled> = {
  frost: 'roughness',
  coat: 'coatWeight',
  subsurface: 'subsurfaceWeight',
  strength: 'emissionStrength',
  anisotropy: 'anisotropic',
  sheen: 'sheenWeight',
  film: 'thinFilmThickness',
  filmIor: 'thinFilmIor',
}

export const DEFAULT_PRESET = 'liquid_glass'

type Params = MaterialSpec['params']

/**
 * Preset defaults overlaid with user overrides (worker presets.material_params): legacy names mapped when the new name
 * is not given, numbers clamped to the schema range, unknown enum values ignored.
 */
export function materialParams(preset: string, overrides: Params | null | undefined, presets: Presets | null | undefined): Principled {
  const schema = presets?.materials?.[preset]?.params ?? presets?.materials?.[DEFAULT_PRESET]?.params
  const out: Record<string, number | string> = { ...DEFAULT_PRINCIPLED }
  if (schema) for (const [k, p] of Object.entries(schema)) if (p.default !== undefined) out[k] = p.default as number | string
  const ov: Record<string, unknown> = { ...(overrides ?? {}) }
  for (const [old, neu] of Object.entries(LEGACY_PARAMS)) {
    if (old in ov && !(neu in ov)) {
      ov[neu] = ov[old]
      delete ov[old]
    }
  }
  for (const [k, v] of Object.entries(ov)) {
    if (v === null || v === undefined || !(k in DEFAULT_PRINCIPLED)) continue
    const p = schema?.[k]
    if (k === 'paintMode') {
      const opts: readonly string[] = p?.options ?? PAINT_MODES
      if (typeof v === 'string' && opts.includes(v)) out[k] = v
      continue
    }
    let n = Number(v)
    if (!Number.isFinite(n)) continue
    if (p?.min !== undefined) n = Math.max(p.min, n)
    if (p?.max !== undefined) n = Math.min(p.max, n)
    out[k] = n
  }
  if (!PAINT_MODES.includes(out.paintMode as PaintMode)) out.paintMode = 'base'
  return out as unknown as Principled
}

/**
 * (preset, full params) of one shape (worker presets.resolve_material): the preset's defaults, then the layer's params,
 * then the shape's own (Layer.elementMaterials[id]). A shape override with ANOTHER preset starts from that preset's
 * defaults. Unknown presets → liquid_glass.
 */
export function resolveMaterial(
  layerMaterial: MaterialSpec | null | undefined,
  elementMaterial: MaterialSpec | null | undefined,
  presets: Presets | null | undefined,
): { preset: string; params: Principled } {
  let preset = String(layerMaterial?.preset || DEFAULT_PRESET)
  let params: Params = { ...(layerMaterial?.params ?? {}) }
  if (elementMaterial) {
    const ep = String(elementMaterial.preset || preset)
    if (ep !== preset) {
      preset = ep
      params = {}
    }
    Object.assign(params, elementMaterial.params ?? {})
  }
  if (presets?.materials && !(preset in presets.materials)) preset = DEFAULT_PRESET
  return { preset, params: materialParams(preset, params, presets) }
}

/** Icon Composer "Effects" off (`glass: false`): the worker renders the layer with the unlit `flat` preset. */
export const FLAT_MATERIAL: MaterialSpec = { preset: 'flat', params: {} }

/** The material of one shape of a layer: layer material (flat when glass is off) + its elementMaterials entry. */
export function shapeMaterial(
  layer: Pick<Layer, 'glass' | 'material' | 'elementMaterials'>,
  elementId: string | null,
  presets: Presets | null | undefined,
  base: MaterialSpec | null = null,
): { preset: string; params: Principled } {
  const glass = layer.glass !== false
  const lm = base ?? (glass ? layer.material : FLAT_MATERIAL)
  const em = glass && elementId ? (layer.elementMaterials?.[elementId] ?? null) : null
  return resolveMaterial(lm, em, presets)
}

/** True when a shape of the layer carries its own material (then touching pieces are not merged into one body). */
export function hasOwnMaterials(layer: Pick<Layer, 'glass' | 'elementMaterials'>, elementIds: Iterable<string>): boolean {
  if (layer.glass === false || !layer.elementMaterials) return false
  for (const id of elementIds) if (layer.elementMaterials[id]) return true
  return false
}

export const isTransmissive = (p: Principled) => p.transmission > 1e-6
export const isEmissive = (p: Principled) => p.paintMode !== 'base' && p.emissionStrength > 0

/** Compositor bloom of the worker (scene info `bloom`): glowing emission above the flat-art level blooms. */
export function bloomAmount(params: Principled[]): number {
  let mx = 0
  for (const p of params) if (p.paintMode !== 'base') mx = Math.max(mx, p.emissionStrength)
  return Math.max(0, Math.min(1, (mx - 1) / 4))
}

// ------------------------------------------------------------------------------------------------ paint
/** Affine art → image UV of a raster image: u = a·x + b·y + c, v = d·x + e·y + f (worker scene.image_uv). */
export type PaintUv = readonly [number, number, number, number, number, number]

/** What paints a shape: a texture covering the −1..1 square of the object (layer art / gradient), or a flat colour. */
export interface PaintBinding {
  map: THREE.Texture | null
  /** Linear colour: the flat paint, or the texture's average (tint mixes of the specular / sheen colour). */
  color: THREE.Color
  /** Raster image placement (art → image UV); null = the art square (u = (x+1)/2, v = (y+1)/2). */
  uv?: PaintUv | null
  /** The texture's alpha multiplies Alpha (raster images, translucent gradient stops). */
  alpha?: boolean
}

/** Tinted renditions (worker env mono): art luminance stretched to floor..1 (linear light) × the tint colour. */
export interface MonoParams {
  lo: number
  hi: number
  floor: number
  tint: [number, number, number]
}

export interface ShapeContext {
  paint: PaintBinding
  /** Piece opacity (layer opacity × region opacity × fill opacity). */
  opacity: number
  /** Body thickness in the mesh's LOCAL units (three.js transmission thickness). */
  thickness: number
  mono?: MonoParams | null
  /**
   * Glass seen through other glass: three.js cannot transmit through a transmissive surface, so a covered glass body
   * is drawn opaque with its base colour × this linear colour (what lies behind it). Null = real transmission.
   */
  covered?: THREE.Color | null
  /** Translucent bodies are drawn in the opaque pass (blendInOpaquePass); false for bodies on top of glass. */
  route?: boolean
  /**
   * 0..1: how much of the surface seen through this (glass) body was lit through it: the body lies on it (stack.ts
   * StackEntry.contact). The transmitted colour then takes Base Color once more (light in, light out).
   */
  contact?: number
}

const placed = new WeakMap<THREE.Texture, Map<string, THREE.Texture>>()
/** A clone of `tex` (same image source, one GPU upload) whose UV transform maps the art square to a raster placement. */
function placedTexture(tex: THREE.Texture, uv: PaintUv): THREE.Texture {
  const key = uv.map((v) => v.toPrecision(9)).join(',')
  let byKey = placed.get(tex)
  if (!byKey) placed.set(tex, (byKey = new Map()))
  let t = byKey.get(key)
  if (!t || t.source !== tex.source) {
    t = tex.clone()
    t.matrixAutoUpdate = false
    const [a, b, c, d, e, f] = uv
    // mesh uv = ((x + 1) / 2, (y + 1) / 2) → x = 2U − 1, y = 2V − 1
    t.matrix.set(2 * a, 2 * b, c - a - b, 2 * d, 2 * e, f - d - e, 0, 0, 1)
    t.needsUpdate = true
    byKey.set(key, t)
  }
  return t
}

const grainMaps = new Map<number, THREE.Texture>()
/** The grain normal map tiled for a Noise scale (Blender Noise Scale s ≈ s cells per art unit; 32 cells per tile). */
function grainMap(scale: number): THREE.Texture {
  const k = Math.round(Math.max(10, scale))
  let t = grainMaps.get(k)
  if (!t) {
    t = getGrainNormalMap().clone()
    const rep = (2 * k) / 32
    t.repeat.set(rep, rep)
    t.needsUpdate = true
    grainMaps.set(k, t)
  }
  return t
}

// ------------------------------------------------------------------------------------------------ the material
const FRAG_PARS = /* glsl */ `
uniform vec3 bisPaintFrom;
uniform float bisTint;
uniform vec3 bisArtColor;
uniform float bisArtAlpha;
uniform float bisEmitArt;
uniform vec4 bisMono;
uniform vec3 bisMonoTint;
uniform vec3 bisBehind;
uniform float bisCovered;
uniform float bisContact;
`

const MAP_FRAGMENT = /* glsl */ `
#ifdef USE_MAP
	vec4 bisArt = texture2D( map, vMapUv );
#else
	vec4 bisArt = vec4( 1.0 );
#endif
	bisArt.rgb *= bisArtColor;
	if ( bisMono.w > 0.5 ) {
		float bisL = dot( bisArt.rgb, vec3( 0.2126, 0.7152, 0.0722 ) );
		bisArt.rgb = mix( bisMono.z, 1.0, clamp( ( bisL - bisMono.x ) / max( bisMono.y - bisMono.x, 1e-3 ), 0.0, 1.0 ) ) * bisMonoTint;
	}
	vec3 bisBase = mix( bisPaintFrom, bisArt.rgb, bisTint );
	// covered glass (drawn opaque): what lies behind it, seen through it (and lit through it: contact)
	diffuseColor.rgb *= mix( bisBase, bisBase * pow( max( bisBase, vec3( 1e-4 ) ), vec3( bisContact ) ) * bisBehind, bisCovered );
	diffuseColor.a *= mix( 1.0, bisArt.a, bisArtAlpha );
`

// Cycles tints transmission by √Base Color per interface, so light crossing a solid body takes Base Color once: the
// thin-surface tint three.js applies. The surface seen through the body was itself lit through it when the body lies
// on it (ShapeContext.contact, caustics on in every Cycles tier): that light takes Base Color once more.
const TRANSMISSION_FRAGMENT = /* glsl */ `
#ifdef USE_TRANSMISSION
	material.diffuseContribution *= pow( max( material.diffuseContribution, vec3( 1e-4 ) ), vec3( bisContact ) );
#endif
#include <transmission_fragment>
`

const EMISSIVE_FRAGMENT = /* glsl */ `
#include <emissivemap_fragment>
	totalEmissiveRadiance *= mix( vec3( 1.0 ), bisArt.rgb, bisEmitArt );
`

function inject(src: string, anchor: string, replacement: string): string {
  if (!src.includes(anchor)) throw new Error(`[materials3d] shader anchor missing: ${anchor}`)
  return src.replace(anchor, replacement)
}

/** MeshPhysicalMaterial + the paint pre-processing of the worker's graph (art mix, mono, emission colour). */
export class PrincipledMaterial extends THREE.MeshPhysicalMaterial {
  readonly bis = {
    bisPaintFrom: { value: new THREE.Color(1, 1, 1) },
    bisTint: { value: 1 },
    bisArtColor: { value: new THREE.Color(1, 1, 1) },
    bisArtAlpha: { value: 0 },
    bisEmitArt: { value: 0 },
    bisMono: { value: new THREE.Vector4(0, 1, 0.08, 0) },
    bisMonoTint: { value: new THREE.Color(1, 1, 1) },
    bisBehind: { value: new THREE.Color(1, 1, 1) },
    bisCovered: { value: 0 },
    bisContact: { value: 0 },
  }
  /** The resolved params last applied (tests / bloom). */
  principled: Principled = { ...DEFAULT_PRINCIPLED }
  covered = false
  /** Translucent body drawn in the opaque pass (needs a back-to-front renderOrder). */
  routed = false

  constructor() {
    super()
    this.onBeforeCompile = (shader) => {
      Object.assign(shader.uniforms, this.bis)
      let fs = shader.fragmentShader
      fs = inject(fs, 'void main() {', `${FRAG_PARS}\nvoid main() {`)
      fs = inject(fs, '#include <map_fragment>', MAP_FRAGMENT)
      fs = inject(fs, '#include <emissivemap_fragment>', EMISSIVE_FRAGMENT)
      fs = inject(fs, '#include <transmission_fragment>', TRANSMISSION_FRAGMENT)
      shader.fragmentShader = fs
    }
  }

  customProgramCacheKey(): string {
    return 'bis-principled-5'
  }
}

const WHITE = new THREE.Color(1, 1, 1)
const BLACK = new THREE.Color(0, 0, 0)
const _c = new THREE.Color()

function mixWhite(out: THREE.Color, art: THREE.Color, t: number): THREE.Color {
  return out.copy(WHITE).lerp(art, Math.max(0, Math.min(1, t)))
}

/** Apply one shape's Principled params + paint to its material (cheap; call on every change). */
export function applyPrincipled(m: PrincipledMaterial, p: Principled, ctx: ShapeContext): void {
  const u = m.bis
  m.principled = p
  const paint = ctx.paint
  const covered = !!ctx.covered && isTransmissive(p)
  m.covered = covered
  const before = programKey(m)

  // ---- paint ------------------------------------------------------------------------------------------
  const map = paint.map ? (paint.uv ? placedTexture(paint.map, paint.uv) : paint.map) : null
  m.map = map
  u.bisArtColor.value.copy(map ? WHITE : paint.color)
  u.bisArtAlpha.value = map && paint.alpha ? 1 : 0
  u.bisPaintFrom.value.copy(p.paintMode === 'emission' ? BLACK : WHITE)
  u.bisTint.value = Math.max(0, Math.min(1, p.tint))
  u.bisEmitArt.value = p.paintMode === 'base' ? 0 : 1
  const mono = ctx.mono
  u.bisMono.value.set(mono?.lo ?? 0, Math.max(mono?.hi ?? 1, (mono?.lo ?? 0) + 1e-3), mono?.floor ?? 0, mono ? 1 : 0)
  u.bisMonoTint.value.setRGB(...(mono?.tint ?? [1, 1, 1]))
  u.bisContact.value = Math.max(0, Math.min(1, ctx.contact ?? 0))
  // representative art colour for the tint mixes three.js only takes as uniforms (specular / sheen colour)
  const artAvg = _c.copy(paint.color)
  if (mono) {
    const l = 0.2126 * artAvg.r + 0.7152 * artAvg.g + 0.0722 * artAvg.b
    const t = Math.max(0, Math.min(1, (l - mono.lo) / Math.max(mono.hi - mono.lo, 1e-3)))
    artAvg.setRGB(...mono.tint).multiplyScalar(mono.floor + (1 - mono.floor) * t)
  }

  // ---- Base / Specular ----------------------------------------------------------------------------------
  m.color.copy(WHITE)
  m.metalness = p.metallic
  m.roughness = p.roughness
  m.ior = Math.max(1, Math.min(2.333, p.ior))
  m.specularIntensity = Math.max(0, 2 * p.specularIorLevel)
  mixWhite(m.specularColor, artAvg, p.specularTint)

  // ---- Transmission -------------------------------------------------------------------------------------
  if (covered) {
    m.transmission = 0
    u.bisBehind.value.copy(ctx.covered!)
    // only the TRANSMITTED share shows what lies behind; the rest stays the lit base (Principled: Transmission Weight
    // mixes diffuse / subsurface and specular transmission). A partly transmissive covered body (the dark renditions'
    // DARK_GLYPH: transmission 0.5) was drawn as base × the dark plate: Earth / Find Device / Weatherbug went black.
    u.bisCovered.value = Math.max(0, Math.min(1, p.transmission))
  } else {
    m.transmission = Math.max(0, Math.min(1, p.transmission))
    u.bisBehind.value.copy(WHITE)
    u.bisCovered.value = 0
  }
  m.thickness = m.transmission > 0 ? Math.max(1e-4, ctx.thickness) : 0
  m.attenuationDistance = Infinity
  m.dispersion = 0 // Blender 5.0's Principled has no dispersion (PLAN §11)

  // ---- Coat / Sheen ---------------------------------------------------------------------------------------
  m.clearcoat = Math.max(0, Math.min(1, p.coatWeight))
  m.clearcoatRoughness = p.coatRoughness
  m.sheen = Math.max(0, Math.min(1, p.sheenWeight))
  m.sheenRoughness = p.sheenRoughness
  mixWhite(m.sheenColor, artAvg, p.sheenTint)

  // ---- Emission ---------------------------------------------------------------------------------------------
  m.emissive.copy(WHITE)
  m.emissiveIntensity = Math.max(0, p.emissionStrength)

  // ---- Thin film / anisotropy / grain -------------------------------------------------------------------
  const film = Math.max(0, p.thinFilmThickness)
  const v = Math.max(0, Math.min(1, p.filmVariation))
  m.iridescence = film > 0 ? 1 : 0
  m.iridescenceIOR = Math.max(1, Math.min(2.333, p.thinFilmIor))
  m.iridescenceThicknessRange = [film * (1 - v), film * (1 + v)]
  m.iridescenceThicknessMap = film > 0 && v > 0 ? getFilmNoiseTexture() : null
  m.anisotropy = Math.max(0, Math.min(1, p.anisotropic))
  m.anisotropyRotation = p.anisotropicRotation * 2 * Math.PI
  m.anisotropyMap = m.anisotropy > 0 ? getRadialAnisotropyMap() : null
  m.normalMap = p.grain > 1e-6 ? grainMap(p.grainScale) : null
  m.normalScale.setScalar(1.5 * Math.max(0, p.grain))

  // ---- Alpha ------------------------------------------------------------------------------------------------
  m.opacity = Math.max(0, Math.min(1, p.alpha * ctx.opacity))
  m.transparent = m.opacity < 0.999 || u.bisArtAlpha.value > 0
  m.depthWrite = true
  m.side = THREE.FrontSide
  if (ctx.route === false) {
    m.blending = THREE.NormalBlending
    m.routed = false
  } else m.routed = blendInOpaquePass(m)
  if (programKey(m) !== before) m.needsUpdate = true
}

/** Everything that changes the compiled program (three.js only recompiles on needsUpdate). */
function programKey(m: THREE.MeshPhysicalMaterial): string {
  return [
    !!m.map,
    m.transmission > 0,
    m.clearcoat > 0,
    m.sheen > 0,
    m.iridescence > 0,
    !!m.iridescenceThicknessMap,
    m.anisotropy > 0,
    !!m.anisotropyMap,
    !!m.normalMap,
    m.transparent,
    m.blending,
    m.dispersion > 0,
  ].join('|')
}

/**
 * three.js draws `transparent` materials after the transmissive (glass) ones and leaves them out of the transmission
 * buffer, so a translucent body lying under a glass layer would vanish behind it (Blender shows it through the glass).
 * Translucent, non-refractive bodies are therefore drawn in the opaque pass: `transparent = false` with an explicit
 * blend function (three.js only drops blending for NormalBlending on opaque materials), back to front by renderOrder.
 * Returns whether the material was routed. Only route bodies that do not lie on top of refracting glass.
 */
export function blendInOpaquePass(m: THREE.MeshPhysicalMaterial): boolean {
  if (!m.transparent || m.transmission > 0) {
    m.blending = THREE.NormalBlending
    return false
  }
  m.blending = THREE.CustomBlending
  m.blendEquation = THREE.AddEquation
  m.blendSrc = THREE.SrcAlphaFactor
  m.blendDst = THREE.OneMinusSrcAlphaFactor
  m.blendEquationAlpha = THREE.AddEquation
  m.blendSrcAlpha = THREE.OneFactor
  m.blendDstAlpha = THREE.OneMinusSrcAlphaFactor
  m.transparent = false
  return true
}

/** renderOrder base of translucent bodies in the opaque pass: plate 1, layer at stack level L → 2 + L. */
export const BLENDED_RENDER_ORDER = { plate: 1, layer: 2 } as const

// The viewport's display transform (Effects.tsx: postprocessing ToneMapping → optional HueSaturation), as plain JS and
// its inverse, plus the GLSL twin of the inverse. The backdrop uses the inverse to paint *scene* colours that come out
// of the tone mapper as exactly the intended *display* colours, e.g. the stage's #0b0b0f instead of the near-black
// the Khronos PBR Neutral curve makes of dark greys (its black-offset subtracts the min channel).
//
// Forward functions mirror three.js r186 `tonemapping_pars_fragment` (LinearToneMapping, NeutralToneMapping,
// AgXToneMapping) and postprocessing 6.39 `hue-saturation.frag`, at exposure 1. Colours are linear sRGB.
import type { RenderSettings } from '../../types'

export type Vec3 = [number, number, number]
/** 0 = linear (Standard), 1 = Khronos PBR Neutral, 2 = AgX, 3 = 'brand' (Standard + highlight soft clip). */
export type ToneMode = 0 | 1 | 2 | 3

export interface DisplayTransform {
  mode: ToneMode
  /** HueSaturation `saturation` after the tone mapper (0 = effect skipped). */
  saturation: number
}

export type ColorModeId = RenderSettings['colorMode']
/** The colour modes the viewport knows (shared/presets.json colorModes). */
export const COLOR_MODES: readonly ColorModeId[] = ['brand', 'neutral', 'standard', 'agx', 'agx-punchy']
/**
 * blender_worker/presets.DEFAULT_COLOR_MODE: the mode the worker renders when a project carries no (or an unknown)
 * render.colorMode (round 5's 'brand'). The live view resolves colour modes the same way.
 */
export const DEFAULT_COLOR_MODE: ColorModeId = 'brand'

/** presets.color_mode_id: a known colour mode, else DEFAULT_COLOR_MODE (missing / unknown modes). */
export function colorModeId(mode: string | null | undefined): ColorModeId {
  return COLOR_MODES.includes(mode as ColorModeId) ? (mode as ColorModeId) : DEFAULT_COLOR_MODE
}

/** The display transform Effects.tsx applies for a project colour mode (keep in sync with it). */
export function displayTransformFor(colorMode: string | null | undefined): DisplayTransform {
  const cm = colorModeId(colorMode)
  if (cm === 'standard') return { mode: 0, saturation: 0 }
  if (cm === 'agx') return { mode: 2, saturation: 0 }
  if (cm === 'agx-punchy') return { mode: 2, saturation: AGX_PUNCHY_SATURATION }
  if (cm === 'brand') return { mode: 3, saturation: 0 }
  return { mode: 1, saturation: 0 }
}

// ------------------------------------------------------------------------------------------------ 'brand'
/**
 * blender_worker/util.BRAND_KNEE / BRAND_CAP ('brand' colour mode): the Standard view transform after a compositor
 * highlight soft clip (render.configure_compositor), per channel in scene-linear light: identity up to the knee, then
 * an exponential roll-off toward 1.0 (C1 at the knee):  y = x (x ≤ k),  y = 1 − (1 − k)·exp(−(x − k)/(1 − k)).
 * Paints are pre-compensated with the exact inverse (targets capped at BRAND_CAP).
 */
export const BRAND_KNEE = 0.9
export const BRAND_CAP = 0.998

/** util.soft_clip: the 'brand' highlight roll-off (scene-linear → display-linear, per channel). */
export function softClip(c: Vec3, knee = BRAND_KNEE): Vec3 {
  const w = 1 - knee
  return c.map((v) => (v <= knee ? v : 1 - w * Math.exp(-(v - knee) / w))) as Vec3
}

/** util.soft_clip_inverse: the radiance 'brand' displays as display-linear `c` (targets capped at `cap`). */
export function softClipInverse(c: Vec3, knee = BRAND_KNEE, cap = BRAND_CAP): Vec3 {
  const w = 1 - knee
  return c.map((v) => {
    const y = Math.min(Math.max(0, v), cap)
    return y <= knee ? y : knee - w * Math.log(1 - (y - knee) / w)
  }) as Vec3
}

/** HueSaturation saturation of the 'agx-punchy' colour mode (Effects.tsx). */
export const AGX_PUNCHY_SATURATION = 0.14

// three.js constants (column-major, as written in GLSL `mat3(col0, col1, col2)`).
export const LINEAR_SRGB_TO_LINEAR_REC2020 = [
  [0.6274, 0.0691, 0.0164],
  [0.3293, 0.9195, 0.088],
  [0.0433, 0.0113, 0.8956],
] as const
export const LINEAR_REC2020_TO_LINEAR_SRGB = [
  [1.6605, -0.1246, -0.0182],
  [-0.5876, 1.1329, -0.1006],
  [-0.0728, -0.0083, 1.1187],
] as const
export const AGX_INSET = [
  [0.856627153315983, 0.137318972929847, 0.11189821299995],
  [0.0951212405381588, 0.761241990602591, 0.0767994186031903],
  [0.0482516061458583, 0.101439036467562, 0.811302368396859],
] as const
export const AGX_OUTSET = [
  [1.1271005818144368, -0.1413297634984383, -0.14132976349843826],
  [-0.11060664309660323, 1.157823702216272, -0.11060664309660294],
  [-0.016493938717834573, -0.016493938717834257, 1.2519364065950405],
] as const
export const AGX_MIN_EV = -12.47393
export const AGX_MAX_EV = 4.026069

type Mat3 = readonly (readonly number[])[]
const mulCols = (m: Mat3, v: Vec3): Vec3 => [
  m[0][0] * v[0] + m[1][0] * v[1] + m[2][0] * v[2],
  m[0][1] * v[0] + m[1][1] * v[1] + m[2][1] * v[2],
  m[0][2] * v[0] + m[1][2] * v[1] + m[2][2] * v[2],
]
const clamp01 = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v)

function agxContrast(x: number): number {
  const x2 = x * x
  const x4 = x2 * x2
  return 15.5 * x4 * x2 - 40.14 * x4 * x + 31.96 * x4 - 6.868 * x2 * x + 0.4298 * x2 + 0.1191 * x - 0.00232
}

export function agxToneMap(c: Vec3): Vec3 {
  let v = mulCols(AGX_INSET, mulCols(LINEAR_SRGB_TO_LINEAR_REC2020, c))
  v = v.map((x) => agxContrast(clamp01((Math.log2(Math.max(x, 1e-10)) - AGX_MIN_EV) / (AGX_MAX_EV - AGX_MIN_EV)))) as Vec3
  v = mulCols(AGX_OUTSET, v)
  v = v.map((x) => Math.max(0, x) ** 2.2) as Vec3
  return mulCols(LINEAR_REC2020_TO_LINEAR_SRGB, v).map(clamp01) as Vec3
}

export function neutralToneMap(c: Vec3): Vec3 {
  const start = 0.8 - 0.04
  const desat = 0.15
  const x = Math.min(c[0], c[1], c[2])
  const offset = x < 0.08 ? x - 6.25 * x * x : 0.04
  let v = c.map((k) => k - offset) as Vec3
  const peak = Math.max(v[0], v[1], v[2])
  if (peak < start) return v
  const d = 1 - start
  const newPeak = 1 - (d * d) / (peak + d - start)
  v = v.map((k) => (k * newPeak) / peak) as Vec3
  const g = 1 - 1 / (desat * (peak - newPeak) + 1)
  return v.map((k) => k + (newPeak - k) * g) as Vec3
}

function saturate(c: Vec3, s: number): Vec3 {
  if (s <= 0) return c
  const avg = (c[0] + c[1] + c[2]) / 3
  const k = 1 - 1 / (1.001 - s)
  return c.map((v) => Math.min(1, v + (avg - v) * k)) as Vec3
}

/** Scene-linear → display-linear, as the viewport's post-processing does it. */
export function toDisplay(scene: Vec3, t: DisplayTransform): Vec3 {
  const tm =
    t.mode === 2
      ? agxToneMap(scene)
      : t.mode === 1
        ? neutralToneMap(scene)
        : t.mode === 3
          ? (softClip(scene.map((v) => Math.max(0, v)) as Vec3).map(clamp01) as Vec3)
          : (scene.map(clamp01) as Vec3)
  return saturate(tm, t.saturation)
}

/** Darkest display value the inverse aims for (AgX's toe maps everything below ~2^-12.5 to 0). */
const DISPLAY_FLOOR = 0.0005

/**
 * Display-linear → scene-linear: the colour to put in the scene so the viewport shows `display`. Exact for the
 * backdrop's range (dark, unsaturated; Neutral below its highlight-compression knee).
 */
export function toScene(display: Vec3, t: DisplayTransform): Vec3 {
  let o = display.map((v) => Math.max(DISPLAY_FLOOR, v)) as Vec3
  if (t.saturation > 0) {
    const avg = (o[0] + o[1] + o[2]) / 3
    const gain = 1 - 1 / (1.001 - t.saturation) // post = c + (avg − c)·gain → c = avg + (post − avg)/(1 − gain)
    o = o.map((v) => avg + (v - avg) / (1 - gain)) as Vec3
  }
  if (t.mode === 1) {
    const om = Math.min(o[0], o[1], o[2])
    // f(c) = c − (m − 6.25 m²) with m = min(c) < 0.08  ⇒  om = 6.25 m²
    const offset = om < 0.04 ? Math.sqrt(om / 6.25) - om : 0.04
    return o.map((v) => v + offset) as Vec3
  }
  if (t.mode === 0) return o
  if (t.mode === 3) return softClipInverse(o)
  // AgX: Newton with a forward-difference Jacobian (converges in ≤ 4 steps for the backdrop's colours).
  let c: Vec3 = [...o]
  for (let it = 0; it < AGX_NEWTON_STEPS; it++) {
    const f0 = agxToneMap(c)
    const cols = [0, 1, 2].map((k) => {
      const h = Math.max(c[k] * 0.01, 1e-6)
      const cc: Vec3 = [...c]
      cc[k] += h
      const f1 = agxToneMap(cc)
      return [(f1[0] - f0[0]) / h, (f1[1] - f0[1]) / h, (f1[2] - f0[2]) / h]
    })
    const d = solve3(cols, [f0[0] - o[0], f0[1] - o[1], f0[2] - o[2]])
    c = [Math.max(1e-7, c[0] - d[0]), Math.max(1e-7, c[1] - d[1]), Math.max(1e-7, c[2] - d[2])]
  }
  return c
}

export const AGX_NEWTON_STEPS = 6

/** Solves J·x = b for J given by columns (Cramer's rule; J is well conditioned here). */
function solve3(cols: number[][], b: Vec3): Vec3 {
  const [a, bb, c] = cols
  const det3 = (x: number[], y: number[], z: number[]) =>
    x[0] * (y[1] * z[2] - y[2] * z[1]) - y[0] * (x[1] * z[2] - x[2] * z[1]) + z[0] * (x[1] * y[2] - x[2] * y[1])
  const det = det3(a, bb, c)
  if (!Number.isFinite(det) || Math.abs(det) < 1e-30) return [0, 0, 0]
  return [det3(b, bb, c) / det, det3(a, b, c) / det, det3(a, bb, b) / det]
}

const glslMat = (m: Mat3) => `mat3(${m.map((c) => `vec3(${c.map((v) => v.toString()).join(', ')})`).join(', ')})`

/**
 * GLSL of `vec3 bisToScene(vec3 displayLinear)` (uniforms `bisToneMode`, `bisSaturation`), the twin of toScene().
 * Requires GLSL ES 3.0 (`inverse`).
 */
export const DISPLAY_INVERSE_GLSL = /* glsl */ `
uniform int bisToneMode;
uniform float bisSaturation;
float bisAgxContrast(float x) {
  float x2 = x * x;
  float x4 = x2 * x2;
  return 15.5 * x4 * x2 - 40.14 * x4 * x + 31.96 * x4 - 6.868 * x2 * x + 0.4298 * x2 + 0.1191 * x - 0.00232;
}
vec3 bisAgx(vec3 c) {
  vec3 v = ${glslMat(AGX_INSET)} * (${glslMat(LINEAR_SRGB_TO_LINEAR_REC2020)} * c);
  v = clamp((log2(max(v, vec3(1e-10))) - (${AGX_MIN_EV})) / (${AGX_MAX_EV} - (${AGX_MIN_EV})), 0.0, 1.0);
  v = vec3(bisAgxContrast(v.x), bisAgxContrast(v.y), bisAgxContrast(v.z));
  v = pow(max(${glslMat(AGX_OUTSET)} * v, vec3(0.0)), vec3(2.2));
  return clamp(${glslMat(LINEAR_REC2020_TO_LINEAR_SRGB)} * v, 0.0, 1.0);
}
vec3 bisToScene(vec3 display) {
  vec3 o = max(display, vec3(${DISPLAY_FLOOR}));
  if (bisSaturation > 0.0) {
    float avg = (o.r + o.g + o.b) / 3.0;
    float gain = 1.0 - 1.0 / (1.001 - bisSaturation);
    o = avg + (o - avg) / (1.0 - gain);
  }
  if (bisToneMode == 1) {
    float om = min(o.r, min(o.g, o.b));
    float offset = om < 0.04 ? sqrt(om / 6.25) - om : 0.04;
    return o + offset;
  }
  if (bisToneMode == 0) return o;
  if (bisToneMode == 3) {
    vec3 y = min(o, vec3(${BRAND_CAP}));
    vec3 hi = ${BRAND_KNEE} - ${(1 - BRAND_KNEE).toFixed(6)} * log(1.0 - max(y - ${BRAND_KNEE}, vec3(0.0)) / ${(1 - BRAND_KNEE).toFixed(6)});
    return mix(y, hi, step(vec3(${BRAND_KNEE}), y));
  }
  vec3 c = o;
  for (int it = 0; it < ${AGX_NEWTON_STEPS}; it++) {
    vec3 f0 = bisAgx(c);
    vec3 h = max(c * 0.01, vec3(1e-6));
    mat3 J = mat3(
      (bisAgx(c + vec3(h.x, 0.0, 0.0)) - f0) / h.x,
      (bisAgx(c + vec3(0.0, h.y, 0.0)) - f0) / h.y,
      (bisAgx(c + vec3(0.0, 0.0, h.z)) - f0) / h.z
    );
    c = max(c - inverse(J) * (f0 - o), vec3(1e-7));
  }
  return c;
}
`

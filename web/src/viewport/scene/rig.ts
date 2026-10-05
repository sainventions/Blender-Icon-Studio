// The light-angle rig shared by the environment, the key light and the rim shader: a port of
// blender_worker/lighting.py (glass doc §6.2, PLAN D1 axes: icon in XY, camera on +Z).
import * as THREE from 'three'
import type { Lighting, Presets } from '../../types'

export interface Rig {
  /** Degrees, 0 = from the top, positive = clockwise (toward +x). Honours the preset's lockAngle. */
  angle: number
  elevation: number
  key: number
  rim: number
  fill: number
  environment: number
  warmth: number
  softness: number
  rimColors: string[]
  intensity: number
}

/**
 * Worker lighting.DIFFUSE_CAL / WORLD_CAL (exposure calibration, QA round 2 #12): the key + fill (diffuse) energy and
 * the world (dome wash, its key softbox spot and front term: diffuse wash + coat sheen on every face) are scaled so a
 * face-on satin plate reads ≈ its SVG colour instead of being pushed into the tone mapper's highlight compression
 * (washed-out brand colours); the grazing rim strips (glass edge highlights) keep their energy.
 */
export const DIFFUSE_CAL = 0.85
export const WORLD_CAL = 0.65
/**
 * Live-view counterpart of the worker's lighting.ENGINE_CAL (round 4). The worker scales every light and the world per
 * render engine so a face-on diffuse surface reads DIFFUSE_A · albedo + DIFFUSE_B (materials3d) in Cycles and EEVEE
 * alike; materials3d pre-compensates paints against exactly that response (displayPaint + diffuseAlbedo). LIVE_CAL
 * scales every live light and the environment so the three.js rig meets the same target. Measured (headless Edge,
 * WebGL, satin plates #3d3d3d … #d5d5d5, fit radiance = A · albedo + B): A 0.883 / B 0.015 at 0.96 → 1.0 / 0.017.
 */
export const LIVE_CAL = 1.088
/**
 * Per-component live calibration (round 5): the face-on diffuse response of each rig component (key, fill, the world's
 * gradient dome, its front term and key softbox spot (all three × environment), the rim strips) measured against Cycles
 * one component at a time (grey satin plate, 256 px, 'brand' mode) so every lighting preset, not only studio, lights a
 * plate like the worker. Multiplies the worker-mirrored energies below (× LIVE_CAL).
 */
export const LIVE_LIGHT_CAL = { key: 1.29, fill: 0.595, dome: 0.935, front: 0.948, softbox: 1.207, rim: 1.0 } as const

/**
 * Typical caster → receiver gap (art units) the key's single VSM blur radius is sized for: a layer's body over the plate
 * or the layer below it (z gap 0.13 − thickness 0.1, or its own 0.1 height). Round 5: was 0.25, which smeared the
 * contact shadows Cycles draws along every glyph's lower edge into a faint wide haze.
 */
export const SHADOW_GAP = 0.05
/**
 * Tall bodies (thick round glass, sphere heads, inflated domes: 0.3-0.5 high) cast from well above the plate: Cycles' big
 * disk key spreads their shadow into a wide, faint penumbra, while the fixed 0.05 gap drew a hard, long, dark slab beside
 * them. The gap follows half the tallest caster's height (default 0.1-thick layers keep SHADOW_GAP), up to this.
 */
export const SHADOW_GAP_MAX = 0.3

/** Caster → receiver gap of the live key's VSM blur for the tallest shadow-casting body (world height). */
export function shadowGap(casterHeight: number | null | undefined): number {
  const h = Number.isFinite(casterHeight) ? (casterHeight as number) : 0
  return Math.max(SHADOW_GAP, Math.min(SHADOW_GAP_MAX, 0.5 * h))
}

/** Linear-light colours used by the worker for warm keys and cool fills. */
export const WARM = new THREE.Color(1.0, 0.82, 0.64)
export const COOL = new THREE.Color(0.72, 0.84, 1.0)

export function resolveRig(lighting: Lighting, presets: Presets | null | undefined): Rig {
  const pre = presets?.lighting?.[lighting.preset] ?? presets?.lighting?.studio
  const clamp01 = (v: number) => Math.max(0, Math.min(1, v))
  return {
    angle: pre?.lockAngle != null ? pre.lockAngle : lighting.angle,
    elevation: Math.max(0, Math.min(89, lighting.elevation)),
    key: (pre?.key ?? 1) * lighting.intensity,
    rim: (pre?.rim ?? 1) * lighting.rim,
    fill: (pre?.fill ?? 1) * lighting.fill,
    environment: (pre?.environment ?? 1) * lighting.environment,
    warmth: pre?.warmth ?? 0,
    softness: clamp01(lighting.shadowSoftness),
    rimColors: pre?.rimColors ?? [],
    intensity: lighting.intensity,
  }
}

/** Unit vector from the icon toward a light: (0,0,1)·cos e + (sin a, cos a, 0)·sin e, in the CAMERA's frame (x =
 *  screen right, y = screen up, z = toward the viewer); viewLightDir turns it into world space. */
export function lightDir(angleDeg: number, elevDeg: number, out = new THREE.Vector3()): THREE.Vector3 {
  const a = THREE.MathUtils.degToRad(angleDeg)
  const e = THREE.MathUtils.degToRad(elevDeg)
  return out.set(Math.sin(a) * Math.sin(e), Math.cos(a) * Math.sin(e), Math.cos(e)).normalize()
}

/**
 * CAMERA-RELATIVE lighting (PLAN §11 round 7, worker lighting.py `view`): the key / fill rig and the studio world are
 * laid out for the head-on view and turned with the camera's world rotation `view` (identity = head-on: camera on +Z
 * looking −Z, +Y up, the same convention as Blender's camera), so the iso / orbit views light like the head-on one
 * and `lighting.angle` is relative to the view (at iso 1 a world-fixed key sat 5° from the mirror direction of the flat
 * tops: glyphs washed to white). World-space unit vector toward a light.
 */
export function viewLightDir(angleDeg: number, elevDeg: number, view: THREE.Quaternion, out = new THREE.Vector3()): THREE.Vector3 {
  return lightDir(angleDeg, elevDeg, out).applyQuaternion(view)
}

const Z_AXIS = new THREE.Vector3(0, 0, 1)
const _q = new THREE.Quaternion()
/**
 * scene.environmentRotation of the studio cube map (rendered for light angle 0 in the head-on frame): the light angle
 * turns it about the view axis (0 = top, clockwise positive: Rz(−angle)), then the camera's rotation carries it into
 * world space (view · Rz(−angle); three.js samples with its inverse).
 */
export function environmentRotation(angleDeg: number, view: THREE.Quaternion, out = new THREE.Euler()): THREE.Euler {
  _q.setFromAxisAngle(Z_AXIS, -THREE.MathUtils.degToRad(angleDeg)).premultiply(view)
  return out.setFromQuaternion(_q)
}

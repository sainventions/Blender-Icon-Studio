// The light-angle rig shared by the environment, the key light and the rim shader — a port of
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
 * the world (dome wash, its key softbox spot and front term — diffuse wash + coat sheen on every face) are scaled so a
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
 * Per-component live calibration (round 5): the face-on diffuse response of each rig component — key, fill, the world's
 * gradient dome, its front term and key softbox spot (all three × environment), the rim strips — measured against Cycles
 * one component at a time (grey satin plate, 256 px, 'brand' mode) so every lighting preset, not only studio, lights a
 * plate like the worker. Multiplies the worker-mirrored energies below (× LIVE_CAL).
 */
export const LIVE_LIGHT_CAL = { key: 1.29, fill: 0.595, dome: 0.935, front: 0.948, softbox: 1.207, rim: 1.0 } as const

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

/** Unit vector from the icon toward a light: (0,0,1)·cos e + (sin a, cos a, 0)·sin e. */
export function lightDir(angleDeg: number, elevDeg: number, out = new THREE.Vector3()): THREE.Vector3 {
  const a = THREE.MathUtils.degToRad(angleDeg)
  const e = THREE.MathUtils.degToRad(elevDeg)
  return out.set(Math.sin(a) * Math.sin(e), Math.cos(a) * Math.sin(e), Math.cos(e)).normalize()
}

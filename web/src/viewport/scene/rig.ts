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

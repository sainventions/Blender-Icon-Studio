// Lighting: the procedural studio environment (rotated by the light angle) + the key light and a cool fill —
// mirroring the worker's rig. The key is a spot at the rig's distance (lighting.KEY_DIST, 12 units since round 5) with
// inverse-square falloff, like Blender's area disk, so the plate gets the same gentle light gradient; its VSM shadow
// map gives the wide, soft penumbra of the (KEY_SCALE × 4 m) softbox.
import { useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import { useThree } from '@react-three/fiber'
import * as THREE from 'three'
import { COOL, DIFFUSE_CAL, LIVE_CAL, LIVE_LIGHT_CAL, WARM, lightDir, type Rig } from './rig'
import { StudioEnvironment } from './studioEnvironment'

interface Props {
  rig: Rig
  /** 0..1 darkness of cast shadows (from the layers' shadow opacity). */
  shadowStrength: number
  /** Whether any layer casts a shadow at all. */
  shadows: boolean
}

/**
 * Worker lighting.KEY_DIST / KEY_SCALE (round 5): the key moved from 6 to 12 units at the same centre irradiance and the
 * same angular size (size × KEY_SCALE), so a flat plate is lit 0.83–1.21× of its centre instead of 0.68–1.46×.
 */
export const KEY_DISTANCE = 12
export const KEY_SCALE = KEY_DISTANCE / 6
const KEY_ANGLE = THREE.MathUtils.degToRad(50)
/** Worker lighting.RIG 'BIS Fill' distance. */
const FILL_DISTANCE = 6
/** Irradiance at the icon centre ≈ KEY_POWER × rig.key / d² (independent of the distance). */
const KEY_POWER = 1.9 * KEY_DISTANCE * KEY_DISTANCE
/** Shadow camera half-width at the icon (art units) and map size → world size of one shadow texel. */
const SHADOW_HALF = 1.75
const SHADOW_MAP = 512
const SHADOW_TEXEL = (2 * SHADOW_HALF) / SHADOW_MAP
const SHADOW_BLUR_MAX = 48
/**
 * Typical caster → receiver gap (art units) the single VSM blur radius is sized for: a layer's body over the plate or
 * the layer below it (z gap 0.13 − thickness 0.1, or its own 0.1 height) — round 5: was 0.25, which smeared the contact
 * shadows Cycles draws along every glyph's lower edge into a faint wide haze.
 */
const SHADOW_GAP = 0.05
/**
 * Only the key casts shadows live, while in Cycles every layer also occludes the studio world (the same shadow-ray
 * transmittance blocks the dome / softbox): next to a glyph Cycles darkens the plate ~1.6× what the key alone takes
 * away (Files' card, Twitter's belly: −24 vs −10 levels). The key's shadow carries that share too.
 */
const SHADOW_ENV_GAIN = 1.6
/** Half depth range of the key's shadow camera around the icon (art units; covers the exploded stack). */
const SHADOW_DEPTH = 4

export function StudioLighting({ rig, shadowStrength, shadows }: Props) {
  const gl = useThree((s) => s.gl)
  const scene = useThree((s) => s.scene)
  const invalidate = useThree((s) => s.invalidate)
  const env = useMemo(() => new StudioEnvironment(256), [])
  const keyRef = useRef<THREE.SpotLight>(null)
  const target = useMemo(() => new THREE.Object3D(), [])

  useEffect(() => {
    return () => {
      if (scene.environment === env.target.texture) scene.environment = null
      env.dispose()
    }
  }, [env, scene])

  // Rebuild + re-render the cube map only when an angle-independent parameter changes.
  const envKey = [
    rig.elevation,
    rig.key,
    rig.rim,
    rig.fill,
    rig.environment,
    rig.warmth,
    rig.intensity,
    rig.rimColors.join(),
  ]
    .map((v) => (typeof v === 'number' ? v.toFixed(3) : v))
    .join('|')
  useLayoutEffect(() => {
    env.configure(rig)
    env.render(gl)
    scene.environment = env.target.texture
    invalidate()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [env, gl, scene, envKey, invalidate])

  // The light angle rotates the environment about the view axis (0 = top, clockwise positive).
  useLayoutEffect(() => {
    scene.environmentRotation.set(0, 0, -THREE.MathUtils.degToRad(rig.angle))
    // Worker ENGINE_CAL: the world is scaled with the lights (rig.ts LIVE_CAL).
    scene.environmentIntensity = LIVE_CAL
    invalidate()
  }, [scene, rig.angle, invalidate])

  const keyDir = lightDir(rig.angle, rig.elevation)
  const fillDir = lightDir(rig.angle + 160, 55)
  const keyColor = useMemo(() => new THREE.Color(1, 1, 1).lerp(WARM, rig.warmth), [rig.warmth])
  const fillColor = useMemo(() => new THREE.Color(1, 1, 1).lerp(COOL, rig.warmth), [rig.warmth])
  // The worker's key is a 4 · KEY_SCALE × (0.3 + 1.4 · softness) m disk at KEY_DISTANCE: penumbra ≈ gap × size /
  // distance. VSM has one blur radius for every receiver, so use the penumbra of a typical layer gap (SHADOW_GAP).
  const keySize = 4 * KEY_SCALE * (0.3 + 1.4 * rig.softness)
  const penumbra = (SHADOW_GAP * keySize) / KEY_DISTANCE
  const radius = Math.max(2, Math.min(SHADOW_BLUR_MAX, penumbra / SHADOW_TEXEL))

  useLayoutEffect(() => {
    const key = keyRef.current
    if (!key) return
    key.target = target
    // Tight shadow frustum around the icon (±1.75 at the icon) instead of the whole 50° cone.
    const half = Math.atan(SHADOW_HALF / KEY_DISTANCE)
    key.shadow.focus = Math.min(1, half / KEY_ANGLE)
    // A tight depth range around the icon: the shadow map stores perspective depth, whose resolution falls with
    // near / d² — with near 1 the 0.1-unit caster → receiver gaps at d = 12 drowned in the bias (no shadows at all).
    key.shadow.camera.near = KEY_DISTANCE - SHADOW_DEPTH
    key.shadow.camera.far = KEY_DISTANCE + SHADOW_DEPTH
    key.shadow.mapSize.set(SHADOW_MAP, SHADOW_MAP)
    key.shadow.bias = -0.003 // VSM self-shadowing acne on flat caps otherwise
    key.shadow.normalBias = 0
    key.shadow.blurSamples = 24
    key.shadow.needsUpdate = true
    invalidate()
  }, [target, invalidate])

  useLayoutEffect(() => {
    const key = keyRef.current
    if (!key) return
    key.shadow.radius = radius
    key.shadow.intensity = Math.max(0, Math.min(1, shadowStrength * SHADOW_ENV_GAIN))
    invalidate()
  }, [radius, shadowStrength, invalidate])

  return (
    <>
      <primitive object={target} />
      <spotLight
        ref={keyRef}
        position={[keyDir.x * KEY_DISTANCE, keyDir.y * KEY_DISTANCE, keyDir.z * KEY_DISTANCE]}
        intensity={KEY_POWER * DIFFUSE_CAL * LIVE_CAL * LIVE_LIGHT_CAL.key * rig.key}
        color={keyColor}
        angle={KEY_ANGLE}
        penumbra={1}
        decay={2}
        distance={0}
        castShadow={shadows}
      />
      {/* Worker 'BIS Fill': an area light FILL_DISTANCE away — a point light with inverse-square falloff, so the plate
          gets the same gentle gradient (its centre irradiance = the former directional fill's). */}
      <pointLight
        position={[fillDir.x * FILL_DISTANCE, fillDir.y * FILL_DISTANCE, fillDir.z * FILL_DISTANCE]}
        intensity={0.35 * FILL_DISTANCE * FILL_DISTANCE * DIFFUSE_CAL * LIVE_CAL * LIVE_LIGHT_CAL.fill * rig.fill}
        color={fillColor}
        decay={2}
        distance={0}
      />
    </>
  )
}

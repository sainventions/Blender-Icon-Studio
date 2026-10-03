// Lighting: the procedural studio environment (rotated by the light angle) + the key light and a cool fill —
// mirroring the worker's rig. The key is a spot at the rig's distance (6 units) with inverse-square falloff, like
// Blender's area disk, so the plate gets the same gentle light gradient; its VSM shadow map gives the wide, soft
// penumbra of a 4 m softbox.
import { useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import { useThree } from '@react-three/fiber'
import * as THREE from 'three'
import { COOL, DIFFUSE_CAL, WARM, lightDir, type Rig } from './rig'
import { StudioEnvironment } from './studioEnvironment'

interface Props {
  rig: Rig
  /** 0..1 darkness of cast shadows (from the layers' shadow opacity). */
  shadowStrength: number
  /** Whether any layer casts a shadow at all. */
  shadows: boolean
}

const KEY_DISTANCE = 6
const KEY_ANGLE = THREE.MathUtils.degToRad(50)
/** Irradiance at the icon centre ≈ KEY_POWER × rig.key / d². */
const KEY_POWER = 1.9 * KEY_DISTANCE * KEY_DISTANCE
/** Shadow camera half-width at the icon (art units) and map size → world size of one shadow texel. */
const SHADOW_HALF = 1.75
const SHADOW_MAP = 512
const SHADOW_TEXEL = (2 * SHADOW_HALF) / SHADOW_MAP
const SHADOW_BLUR_MAX = 48

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
    scene.environmentIntensity = 1
    invalidate()
  }, [scene, rig.angle, invalidate])

  const keyDir = lightDir(rig.angle, rig.elevation)
  const fillDir = lightDir(rig.angle + 160, 55)
  const keyColor = useMemo(() => new THREE.Color(1, 1, 1).lerp(WARM, rig.warmth), [rig.warmth])
  const fillColor = useMemo(() => new THREE.Color(1, 1, 1).lerp(COOL, rig.warmth), [rig.warmth])
  // The worker's key is a 4 × (0.3 + 1.4 · softness) m disk at 6 m: penumbra ≈ gap × size / 6. VSM has one blur
  // radius for every receiver, so use the penumbra of a typical layer gap (~0.25 art units).
  const keySize = 4 * (0.3 + 1.4 * rig.softness)
  const penumbra = (0.25 * keySize) / KEY_DISTANCE
  const radius = Math.max(2, Math.min(SHADOW_BLUR_MAX, penumbra / SHADOW_TEXEL))

  useLayoutEffect(() => {
    const key = keyRef.current
    if (!key) return
    key.target = target
    // Tight shadow frustum around the icon (±1.75 at the icon) instead of the whole 50° cone.
    const half = Math.atan(SHADOW_HALF / KEY_DISTANCE)
    key.shadow.focus = Math.min(1, half / KEY_ANGLE)
    key.shadow.camera.near = 1
    key.shadow.camera.far = 20
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
    key.shadow.intensity = Math.max(0, Math.min(1, shadowStrength))
    invalidate()
  }, [radius, shadowStrength, invalidate])

  return (
    <>
      <primitive object={target} />
      <spotLight
        ref={keyRef}
        position={[keyDir.x * KEY_DISTANCE, keyDir.y * KEY_DISTANCE, keyDir.z * KEY_DISTANCE]}
        intensity={KEY_POWER * DIFFUSE_CAL * rig.key}
        color={keyColor}
        angle={KEY_ANGLE}
        penumbra={1}
        decay={2}
        distance={0}
        castShadow={shadows}
      />
      <directionalLight
        position={[fillDir.x * 10, fillDir.y * 10, fillDir.z * 10]}
        intensity={0.35 * DIFFUSE_CAL * rig.fill}
        color={fillColor}
      />
    </>
  )
}

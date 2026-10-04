// Lighting: the procedural studio environment (rotated by the light angle) + the key light and a cool fill —
// mirroring the worker's rig, CAMERA-RELATIVE like it (PLAN §11 round 7): rig and environment are laid out for the
// head-on view and turned with the camera every frame the camera turns (iso swing, orbit), so the iso view lights like
// the head-on one and the light angle is relative to the view. The key is a spot at the rig's distance (lighting.KEY_DIST, 12 units since round 5) with
// inverse-square falloff, like Blender's area disk, so the plate gets the same gentle light gradient; its VSM shadow
// map gives the wide, soft penumbra of the (KEY_SCALE × 4 m) softbox.
import { useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import { useFrame, useThree } from '@react-three/fiber'
import * as THREE from 'three'
import { COOL, DIFFUSE_CAL, LIVE_CAL, LIVE_LIGHT_CAL, WARM, environmentRotation, shadowGap, viewLightDir, type Rig } from './rig'
import { StudioEnvironment } from './studioEnvironment'

interface Props {
  rig: Rig
  /** 0..1 darkness of the key's cast shadows (0 = no layer casts one; glass casts lighter shadows). */
  shadowIntensity: number
  /**
   * World height of the tallest shadow-casting body (thickness, or an inflated dome's full height). Its silhouette's
   * upper edge casts from about half that height, so the soft key's penumbra grows with it (rig.shadowGap).
   */
  casterHeight?: number
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
/** Half depth range of the key's shadow camera around the icon (art units; covers the whole stack). */
const SHADOW_DEPTH = 4

export function StudioLighting({ rig, shadowIntensity, casterHeight = 0 }: Props) {
  const shadows = shadowIntensity > 0
  const gl = useThree((s) => s.gl)
  const scene = useThree((s) => s.scene)
  const invalidate = useThree((s) => s.invalidate)
  const env = useMemo(() => new StudioEnvironment(256), [])
  const keyRef = useRef<THREE.SpotLight>(null)
  const fillRef = useRef<THREE.PointLight>(null)
  const target = useMemo(() => new THREE.Object3D(), [])
  /** The camera rotation / light angle the rig was last laid out for (NaN: not yet). */
  const laid = useRef({ q: new THREE.Quaternion(NaN, NaN, NaN, NaN), angle: NaN, elevation: NaN, dir: new THREE.Vector3() })

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

  // Worker ENGINE_CAL: the world is scaled with the lights (rig.ts LIVE_CAL).
  useLayoutEffect(() => {
    scene.environmentIntensity = LIVE_CAL
    invalidate()
  }, [scene, invalidate])
  // a new light angle / elevation: lay the rig out again on the next frame
  useLayoutEffect(() => invalidate(), [rig.angle, rig.elevation, invalidate])

  // Camera-relative rig (runs after the camera rigs' frame callbacks, before the render): the light angle turns the
  // environment about the view axis (0 = top, clockwise positive) and the camera's rotation carries environment, key
  // and fill into world space — only when the camera turned or the angle changed.
  useFrame((state) => {
    const L = laid.current
    const q = state.camera.quaternion
    if (L.angle === rig.angle && L.elevation === rig.elevation && Math.abs(L.q.dot(q)) > 1 - 1e-12) return
    L.q.copy(q)
    L.angle = rig.angle
    L.elevation = rig.elevation
    environmentRotation(rig.angle, q, scene.environmentRotation)
    const key = keyRef.current
    if (key) key.position.copy(viewLightDir(rig.angle, rig.elevation, q, L.dir)).multiplyScalar(KEY_DISTANCE)
    const fill = fillRef.current
    if (fill) fill.position.copy(viewLightDir(rig.angle + 160, 55, q, L.dir)).multiplyScalar(FILL_DISTANCE)
  })
  const keyColor = useMemo(() => new THREE.Color(1, 1, 1).lerp(WARM, rig.warmth), [rig.warmth])
  const fillColor = useMemo(() => new THREE.Color(1, 1, 1).lerp(COOL, rig.warmth), [rig.warmth])
  // The worker's key is a 4 · KEY_SCALE × (0.3 + 1.4 · softness) m disk at KEY_DISTANCE: penumbra ≈ gap × size /
  // distance. VSM has one blur radius for every receiver: the penumbra of a typical caster → receiver gap (rig.shadowGap).
  const keySize = 4 * KEY_SCALE * (0.3 + 1.4 * rig.softness)
  const penumbra = (shadowGap(casterHeight) * keySize) / KEY_DISTANCE
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
    key.shadow.intensity = Math.max(0, Math.min(1, shadowIntensity))
    invalidate()
  }, [radius, shadowIntensity, invalidate])

  return (
    <>
      <primitive object={target} />
      <spotLight
        ref={keyRef}
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
        ref={fillRef}
        intensity={0.35 * FILL_DISTANCE * FILL_DISTANCE * DIFFUSE_CAL * LIVE_CAL * LIVE_LIGHT_CAL.fill * rig.fill}
        color={fillColor}
        decay={2}
        distance={0}
      />
    </>
  )
}

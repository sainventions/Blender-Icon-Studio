// Cameras. Front view mirrors the Blender camera exactly (orthographic, ortho_scale = 2.24 / zoom, looking −Z);
// as the UI explode grows it swings into a gentle three-quarter view and frames the spread stack. Orbit view is a
// perspective camera with damped, angle-limited OrbitControls (double-click empty space to reset).
import { useEffect, useLayoutEffect, useRef } from 'react'
import { useFrame, useThree } from '@react-three/fiber'
import { OrbitControls, OrthographicCamera, PerspectiveCamera } from '@react-three/drei'
import * as THREE from 'three'
import type { OrbitControls as OrbitControlsImpl } from 'three-stdlib'
import { damp, useViewportStore } from './store'
import type { ZBox } from './LayerStack'

/** Front framing: the plate (2 units) plus a 12 % margin for shadows and glow. */
export const FRONT_ORTHO_SCALE = 2.24

const EXPLODE_YAW = THREE.MathUtils.degToRad(-42)
const EXPLODE_PITCH = THREE.MathUtils.degToRad(24)

interface Props {
  view: 'front' | 'orbit'
  zoom: number
  fov: number
  /** Top of the layer stack (z) for a given explode amount. */
  stackTop: (explode: number) => number
  /** World boxes of plate + layers for a given explode amount (exploded framing). */
  boxes: (explode: number) => ZBox[]
  plateThickness: number
}

export function CameraRig(p: Props) {
  return p.view === 'orbit' ? <OrbitRig {...p} /> : <FrontRig {...p} />
}

function FrontRig({ zoom, boxes }: Props) {
  const ref = useRef<THREE.OrthographicCamera>(null)
  const store = useViewportStore()
  const v = useRef({
    dir: new THREE.Vector3(),
    right: new THREE.Vector3(),
    up: new THREE.Vector3(),
    target: new THREE.Vector3(),
    corner: new THREE.Vector3(),
  })

  useFrame((state) => {
    const cam = ref.current
    if (!cam) return
    const e = store.explode.current
    const { width, height } = state.size
    const k = v.current
    const yaw = EXPLODE_YAW * e
    const pitch = EXPLODE_PITCH * e
    k.dir.set(Math.sin(yaw) * Math.cos(pitch), Math.sin(pitch), Math.cos(yaw) * Math.cos(pitch))
    k.right.set(0, 1, 0).cross(k.dir).normalize()
    k.up.copy(k.dir).cross(k.right).normalize()

    // e = 0 is exactly the Blender camera (ortho_scale 2.24 / zoom, centred). As the stack explodes, frame the
    // projected extents of plate + layers so the three-quarter view stays centred and fully visible.
    const fixedFrame = FRONT_ORTHO_SCALE / Math.max(0.05, zoom)
    let cx = 0
    let cy = 0
    let frameW = fixedFrame
    let frameH = fixedFrame
    if (e > 1e-4) {
      let minX = Infinity
      let maxX = -Infinity
      let minY = Infinity
      let maxY = -Infinity
      for (const b of boxes(e)) {
        for (let i = 0; i < 8; i++) {
          k.corner.set(i & 1 ? b.x1 : b.x0, i & 2 ? b.y1 : b.y0, i & 4 ? b.z1 : b.z0)
          const px = k.corner.dot(k.right)
          const py = k.corner.dot(k.up)
          if (px < minX) minX = px
          if (px > maxX) maxX = px
          if (py < minY) minY = py
          if (py > maxY) maxY = py
        }
      }
      if (Number.isFinite(minX)) {
        const margin = 1.16 / Math.max(0.05, zoom)
        cx = ((minX + maxX) / 2) * e
        cy = ((minY + maxY) / 2) * e
        frameW = fixedFrame + ((maxX - minX) * margin - fixedFrame) * e
        frameH = fixedFrame + ((maxY - minY) * margin - fixedFrame) * e
      }
    }
    k.target.copy(k.right).multiplyScalar(cx).addScaledVector(k.up, cy)
    cam.position.copy(k.target).addScaledVector(k.dir, 20)
    cam.up.set(0, 1, 0)
    cam.lookAt(k.target)
    const z = Math.min(width / Math.max(frameW, 1e-3), height / Math.max(frameH, 1e-3))
    if (Math.abs(cam.zoom - z) > 1e-6) {
      cam.zoom = z
      cam.updateProjectionMatrix()
    }
  }, -1)

  return <OrthographicCamera ref={ref} makeDefault near={0.1} far={60} position={[0, 0, 20]} />
}

function OrbitRig({ zoom, fov, stackTop, plateThickness }: Props) {
  const camRef = useRef<THREE.PerspectiveCamera>(null)
  const controlsRef = useRef<OrbitControlsImpl>(null)
  const store = useViewportStore()
  const size = useThree((s) => s.size)
  const gl = useThree((s) => s.gl)
  const invalidate = useThree((s) => s.invalidate)
  const reset = useRef(false)
  const home = useRef(new THREE.Vector3())

  const distance = () => {
    const half = FRONT_ORTHO_SCALE / Math.max(0.05, zoom) / 2
    const aspect = size.width / Math.max(1, size.height)
    const t = Math.tan(THREE.MathUtils.degToRad(fov) / 2)
    return (half / t) * (aspect < 1 ? 1 / aspect : 1) + 0.6
  }

  const homePosition = (out: THREE.Vector3) => {
    const d = distance()
    const yaw = THREE.MathUtils.degToRad(-26)
    const pitch = THREE.MathUtils.degToRad(15)
    return out.set(d * Math.sin(yaw) * Math.cos(pitch), d * Math.sin(pitch), d * Math.cos(yaw) * Math.cos(pitch))
  }

  useLayoutEffect(() => {
    const cam = camRef.current
    const ctl = controlsRef.current
    if (!cam || !ctl) return
    homePosition(cam.position)
    ctl.target.set(0, 0, 0)
    ctl.update()
    invalidate()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    const el = gl.domElement
    const onDbl = () => {
      reset.current = true
      invalidate()
    }
    el.addEventListener('dblclick', onDbl)
    return () => el.removeEventListener('dblclick', onDbl)
  }, [gl, invalidate])

  useFrame((state, dt) => {
    const ctl = controlsRef.current
    const cam = camRef.current
    if (!ctl || !cam) return
    const e = store.explode.current
    const cz = ((stackTop(e) - plateThickness) / 2) * Math.min(1, e * 1.5)
    if (Math.abs(ctl.target.z - cz) > 1e-5) {
      ctl.target.z = cz
      ctl.update()
    }
    if (reset.current) {
      homePosition(home.current)
      home.current.z += cz
      cam.position.set(
        damp(cam.position.x, home.current.x, 10, dt),
        damp(cam.position.y, home.current.y, 10, dt),
        damp(cam.position.z, home.current.z, 10, dt),
      )
      ctl.target.x = damp(ctl.target.x, 0, 10, dt)
      ctl.target.y = damp(ctl.target.y, 0, 10, dt)
      ctl.update()
      if (cam.position.distanceTo(home.current) < 1e-3 && Math.hypot(ctl.target.x, ctl.target.y) < 1e-3)
        reset.current = false
      state.invalidate()
    }
  }, -1)

  const d = distance()
  return (
    <>
      <PerspectiveCamera ref={camRef} makeDefault fov={fov} near={0.05} far={100} />
      <OrbitControls
        ref={controlsRef}
        makeDefault
        enableDamping
        dampingFactor={0.09}
        rotateSpeed={0.75}
        zoomSpeed={0.8}
        panSpeed={0.7}
        minPolarAngle={THREE.MathUtils.degToRad(18)}
        maxPolarAngle={THREE.MathUtils.degToRad(162)}
        minAzimuthAngle={THREE.MathUtils.degToRad(-80)}
        maxAzimuthAngle={THREE.MathUtils.degToRad(80)}
        minDistance={d * 0.35}
        maxDistance={d * 2.6}
      />
    </>
  )
}

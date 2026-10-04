// Cameras. The front view is the CAD-style iso view (PLAN §11, scene/iso.ts): an orthographic camera turned from
// head-on (iso 0 — exactly the Blender camera: ortho_scale 2.24 / zoom, centred, looking −Z) toward the isometric view
// (iso 1) by a quaternion slerp, showing the layers at their REAL z distances and auto-framed like the worker's
// framing.ortho_plan. The iso amount and the framing are damped, so a slider drag animates smoothly. Orbit view is a
// perspective camera with damped, angle-limited OrbitControls pivoting about the centre of plate + layers, at the
// distance that fits them into the frustum for the current view direction; the user's orbit / zoom / pan ride on that
// fit as offsets (double-click empty space to return home).
import { useEffect, useRef } from 'react'
import { useFrame, useThree } from '@react-three/fiber'
import { OrbitControls, OrthographicCamera, PerspectiveCamera } from '@react-three/drei'
import * as THREE from 'three'
import type { OrbitControls as OrbitControlsImpl } from 'three-stdlib'
import { FRONT_ORTHO_SCALE, isoBasis, isoQuaternion, orthoFrame } from './iso'
import { damp, useViewportStore } from './store'

export { FRONT_ORTHO_SCALE }

/** Damping rate (1/s) of framing changes (stack edits, iso, resize). */
const FRAME_LAMBDA = 11
/** Damping rate (1/s) of the iso amount (head-on ↔ isometric). */
const ISO_LAMBDA = 7
/** Iso amounts below this count as 0: the front camera is exactly the Blender camera. */
const ISO_EPS = 1e-4
/** Orbit view: frustum-fit margin, home direction and the user's dolly range relative to the fit distance. */
const ORBIT_MARGIN = 1.12
const HOME_YAW = THREE.MathUtils.degToRad(-26)
const HOME_PITCH = THREE.MathUtils.degToRad(15)
const MIN_DOLLY = 0.35
const MAX_DOLLY = 2.6
/** While the framing lags behind a growing stack, keep at least the hull + this margin in view. */
const MIN_MARGIN = 1.04

/** Writes world-space xyz triplets of the framing hull (plate outline + layer hulls) into `out`; returns the count. */
export type FramePointsFn = (out: number[]) => number

interface Props {
  view: 'front' | 'orbit'
  zoom: number
  fov: number
  /** Target CAD iso amount (0..1). */
  iso: number
  points: FramePointsFn
}

export function CameraRig(p: Props) {
  return p.view === 'orbit' ? <OrbitRig {...p} /> : <IsoRig {...p} />
}

/** Damps `current` toward `target` and snaps once within a relative 1e-6 (exact convergence). */
function approach(current: number, target: number, dt: number, lambda = FRAME_LAMBDA): number {
  const next = damp(current, target, lambda, dt)
  return Math.abs(next - target) <= 1e-6 * Math.max(1, Math.abs(target)) ? target : next
}

function IsoRig({ zoom, iso, points }: Props) {
  const ref = useRef<THREE.OrthographicCamera>(null)
  const store = useViewportStore()
  const invalidate = useThree((s) => s.invalidate)
  const k = useRef({
    pts: [] as number[],
    q: new THREE.Quaternion(),
    target: new THREE.Vector3(),
    frame: null as { tx: number; ty: number; tz: number; cx: number; cy: number; scale: number } | null,
  }).current

  useEffect(() => {
    store.iso.target = Math.max(0, Math.min(1, Number.isFinite(iso) ? iso : 0))
    invalidate()
  }, [store, iso, invalidate])

  useFrame((state, dt) => {
    const cam = ref.current
    if (!cam) return
    const s = store.iso
    if (s.current !== s.target) {
      const next = damp(s.current, s.target, ISO_LAMBDA, dt)
      s.current = Math.abs(next - s.target) < 5e-4 ? s.target : next
    }
    const t = s.current
    const n = points(k.pts)
    const zf = Math.max(0.05, zoom)
    // iso 0 is exactly the Blender front camera; otherwise framing.ortho_plan (real distances)
    const goal =
      t > ISO_EPS ? orthoFrame(k.pts, n, t, zf) : { target: new THREE.Vector3(), cx: 0, cy: 0, scale: FRONT_ORTHO_SCALE / zf, reach: 1 }
    let f = k.frame
    if (!f) f = k.frame = { tx: goal.target.x, ty: goal.target.y, tz: goal.target.z, cx: goal.cx, cy: goal.cy, scale: goal.scale }
    else {
      f.tx = approach(f.tx, goal.target.x, dt)
      f.ty = approach(f.ty, goal.target.y, dt)
      f.tz = approach(f.tz, goal.target.z, dt)
      f.cx = approach(f.cx, goal.cx, dt)
      f.cy = approach(f.cy, goal.cy, dt)
      f.scale = approach(f.scale, goal.scale, dt)
    }
    const moving =
      s.current !== s.target ||
      f.tx !== goal.target.x ||
      f.ty !== goal.target.y ||
      f.tz !== goal.target.z ||
      f.cx !== goal.cx ||
      f.cy !== goal.cy ||
      f.scale !== goal.scale
    isoQuaternion(t, k.q)
    const { x: ax, y: ay, z: az } = isoBasis(t)
    // camera on the view axis through the frame centre (lens shift = moving the ortho camera sideways)
    k.target.set(f.tx, f.ty, f.tz).addScaledVector(ax, f.cx).addScaledVector(ay, f.cy)
    const dist = Math.max(20, goal.reach + 5)
    cam.position.copy(k.target).addScaledVector(az, dist)
    cam.quaternion.copy(k.q)
    const { width, height } = state.size
    // the square Blender frame (side `scale`) on the viewport's shorter side
    const zz = Math.min(width, height) / Math.max(1e-6, f.scale)
    const far = dist + goal.reach + 10
    if (Math.abs(cam.zoom - zz) > 1e-6 || cam.far !== far) {
      cam.zoom = zz
      cam.far = far
      cam.updateProjectionMatrix()
    }
    if (moving) state.invalidate()
  }, -1)

  return <OrthographicCamera ref={ref} makeDefault near={0.1} far={60} position={[0, 0, 20]} />
}

function homeOffset(dist: number, out: THREE.Vector3): THREE.Vector3 {
  return out.set(
    dist * Math.sin(HOME_YAW) * Math.cos(HOME_PITCH),
    dist * Math.sin(HOME_PITCH),
    dist * Math.cos(HOME_YAW) * Math.cos(HOME_PITCH),
  )
}

function OrbitRig({ zoom, fov, points }: Props) {
  const camRef = useRef<THREE.PerspectiveCamera>(null)
  const controlsRef = useRef<OrbitControlsImpl>(null)
  const gl = useThree((s) => s.gl)
  const invalidate = useThree((s) => s.invalidate)
  const k = useRef({
    pts: [] as number[],
    /** Centre of the hull's bounding box. */
    box: new THREE.Vector3(),
    goalCenter: new THREE.Vector3(),
    center: new THREE.Vector3(),
    prevCenter: new THREE.Vector3(),
    offset: new THREE.Vector3(),
    home: new THREE.Vector3(),
    dir: new THREE.Vector3(),
    right: new THREE.Vector3(),
    up: new THREE.Vector3(),
    /** Hull points in view coordinates about the box centre (right, up, toward-camera). */
    view: new Float64Array(0),
    dist: 0,
    init: false,
    reset: false,
  }).current

  useEffect(() => {
    const el = gl.domElement
    const onDbl = () => {
      k.reset = true
      invalidate()
    }
    el.addEventListener('dblclick', onDbl)
    return () => el.removeEventListener('dblclick', onDbl)
  }, [gl, invalidate, k])

  // Runs before OrbitControls.update() (priority −1), which then applies the user's input on top of the fit.
  useFrame((state, dt) => {
    const ctl = controlsRef.current
    const cam = camRef.current
    if (!ctl || !cam) return
    // Pivot = centre of the hull's bounding box (view-independent, so orbiting turns about a fixed point).
    const n = points(k.pts)
    const P = k.pts
    let x0 = Infinity
    let y0 = Infinity
    let z0 = Infinity
    let x1 = -Infinity
    let y1 = -Infinity
    let z1 = -Infinity
    for (let i = 0; i < n; i++) {
      const x = P[i * 3]
      const y = P[i * 3 + 1]
      const z = P[i * 3 + 2]
      if (x < x0) x0 = x
      if (x > x1) x1 = x
      if (y < y0) y0 = y
      if (y > y1) y1 = y
      if (z < z0) z0 = z
      if (z > z1) z1 = z
    }
    if (n > 0) k.box.set((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)
    else k.box.set(0, 0, 0)
    k.goalCenter.copy(k.box)

    // Fit distance for the current view direction: the smallest D (camera at pivot + dir·D) that puts every hull
    // point inside the frustum with margin: D ≥ depth + m·|x| / tan(hfov/2) and D ≥ depth + m·|y| / tan(vfov/2).
    if (k.init) k.dir.subVectors(cam.position, ctl.target)
    if (!k.init || k.dir.lengthSq() < 1e-12) homeOffset(1, k.dir)
    k.dir.normalize()
    k.right.set(0, 1, 0).cross(k.dir)
    if (k.right.lengthSq() < 1e-8) k.right.set(1, 0, 0)
    k.right.normalize()
    k.up.copy(k.dir).cross(k.right).normalize()
    const { width, height } = state.size
    const aspect = width / Math.max(1, height)
    const tanV = Math.tan(THREE.MathUtils.degToRad(Math.max(5, Math.min(120, fov))) / 2)
    const tanH = tanV * aspect
    const m = ORBIT_MARGIN / Math.max(0.05, zoom)
    const c = k.goalCenter
    if (k.view.length < n * 3) k.view = new Float64Array(n * 3 + 96)
    const V = k.view
    let front = -Infinity
    let radius = 0
    for (let i = 0; i < n; i++) {
      const dx = P[i * 3] - c.x
      const dy = P[i * 3 + 1] - c.y
      const dz = P[i * 3 + 2] - c.z
      V[i * 3] = dx * k.right.x + dy * k.right.y + dz * k.right.z
      V[i * 3 + 1] = dx * k.up.x + dy * k.up.y + dz * k.up.z
      V[i * 3 + 2] = dx * k.dir.x + dy * k.dir.y + dz * k.dir.z
      front = Math.max(front, V[i * 3 + 2])
      radius = Math.max(radius, Math.sqrt(dx * dx + dy * dy + dz * dz))
    }
    /** Fit distance about the box centre shifted by (sx, sy, sd) in view coordinates, with margin `mm`. */
    const fitAt = (sx: number, sy: number, sd = 0, mm = m) => {
      let d = 0
      for (let i = 0; i < n; i++) {
        const pd = V[i * 3 + 2] - sd
        d = Math.max(d, pd + (mm * Math.abs(V[i * 3] - sx)) / tanH, pd + (mm * Math.abs(V[i * 3 + 1] - sy)) / tanV)
      }
      return d
    }
    let goalDist = fitAt(0, 0)
    if (n > 0) {
      // Perspective makes the hull asymmetric about the box centre: shift the pivot sideways (in the view plane) by
      // the offset of the projected hull's centre at that distance and refit (twice) — the stack ends up centred.
      let sx = 0
      let sy = 0
      for (let pass = 0; pass < 2; pass++) {
        let t0 = Infinity
        let t1 = -Infinity
        let u0 = Infinity
        let u1 = -Infinity
        for (let i = 0; i < n; i++) {
          const z = Math.max(1e-3, goalDist - V[i * 3 + 2])
          const tx = (V[i * 3] - sx) / z
          const ty = (V[i * 3 + 1] - sy) / z
          if (tx < t0) t0 = tx
          if (tx > t1) t1 = tx
          if (ty < u0) u0 = ty
          if (ty > u1) u1 = ty
        }
        sx += ((t0 + t1) / 2) * goalDist
        sy += ((u0 + u1) / 2) * goalDist
        goalDist = fitAt(sx, sy)
      }
      c.addScaledVector(k.right, sx).addScaledVector(k.up, sy)
    } else {
      radius = FRONT_ORTHO_SCALE / 2
      goalDist = (m * radius) / Math.min(tanV, tanH)
    }
    // Never inside the stack (extreme zoom values).
    goalDist = Math.max(goalDist, (Number.isFinite(front) ? front : 0) + 0.25)

    if (!k.init) {
      k.init = true
      k.center.copy(k.goalCenter)
      k.dist = goalDist
      ctl.target.copy(k.center)
      cam.position.copy(k.center).add(homeOffset(k.dist, k.offset))
    } else {
      // Damped fit; the user's orbit / dolly / pan ride on it as offsets: the target and camera move with the centre,
      // the camera's distance from the target scales with the fit distance.
      k.prevCenter.copy(k.center)
      const prevDist = k.dist
      k.center.set(
        approach(k.center.x, k.goalCenter.x, dt),
        approach(k.center.y, k.goalCenter.y, dt),
        approach(k.center.z, k.goalCenter.z, dt),
      )
      k.dist = approach(k.dist, goalDist, dt)
      if (n > 0) {
        // Never let the lag crop a fast-growing stack: fit (with a small margin) about the pivot actually in use.
        k.offset.subVectors(k.center, k.box)
        const need = fitAt(
          k.offset.dot(k.right),
          k.offset.dot(k.up),
          k.offset.dot(k.dir),
          MIN_MARGIN / Math.max(0.05, zoom),
        )
        k.dist = Math.max(k.dist, need)
      }
      if (!k.center.equals(k.prevCenter) || k.dist !== prevDist) {
        k.offset.subVectors(cam.position, ctl.target).multiplyScalar(k.dist / Math.max(1e-6, prevDist))
        ctl.target.add(k.prevCenter.sub(k.center).negate())
        cam.position.copy(ctl.target).add(k.offset)
      }
    }
    const moving = !k.center.equals(k.goalCenter) || k.dist !== goalDist

    ctl.minDistance = k.dist * MIN_DOLLY
    ctl.maxDistance = k.dist * MAX_DOLLY
    const near = Math.max(0.05, k.dist * 0.01)
    const far = Math.max(100, (k.dist * MAX_DOLLY + radius) * 1.5)
    if (cam.near !== near || cam.far !== far) {
      cam.near = near
      cam.far = far
      cam.updateProjectionMatrix()
    }

    if (k.reset) {
      k.home.copy(k.center).add(homeOffset(k.dist, k.offset))
      cam.position.set(
        damp(cam.position.x, k.home.x, 10, dt),
        damp(cam.position.y, k.home.y, 10, dt),
        damp(cam.position.z, k.home.z, 10, dt),
      )
      ctl.target.set(
        damp(ctl.target.x, k.center.x, 10, dt),
        damp(ctl.target.y, k.center.y, 10, dt),
        damp(ctl.target.z, k.center.z, 10, dt),
      )
      if (cam.position.distanceTo(k.home) < 1e-3 && ctl.target.distanceTo(k.center) < 1e-3) k.reset = false
      state.invalidate()
    }
    if (moving) state.invalidate()
  }, -2)

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
      />
    </>
  )
}

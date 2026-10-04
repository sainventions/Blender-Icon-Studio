// CAD-style iso view (PLAN §11 View) — the live twin of blender_worker/framing.py's iso_basis / ortho_plan.
//
// The head-on view is the CAD "top" view of the icon lying flat (stack axis +Z toward the viewer, art +Y up).
// camera.iso t in 0..1 turns an ORTHOGRAPHIC camera along the shortest rotation (quaternion slerp) from that basis to
// the standard isometric one: view direction (1, −1, 1)/√3 (elevation 35.264°, azimuth 45°), screen up = the stack
// axis (camera x = (1, 1, 0)/√2, y = (−1, 1, 2)/√6). Layers keep their REAL z (no explode spreading). The frame fits
// the subject (every visible piece's hull at its back and front z; the plate outline spans −1..1; without a plate the
// canvas square stands in) with the front view's own border, so t → 0 continues the front framing (2.24 / zoom).
import * as THREE from 'three'

/** The front view's border (plate edge 5.4 % from the frame) — framing.FRONT_MARGIN. */
export const FRONT_MARGIN = (1 - 2.0 / 2.24) / 2
/** Head-on framing: ortho_scale = FRONT_ORTHO_SCALE / zoom. */
export const FRONT_ORTHO_SCALE = 2.24

/** Columns: camera x, y, z at iso 1 (framing.ISO_ROT). */
export const ISO_ROT = new THREE.Matrix4().makeBasis(
  new THREE.Vector3(1, 1, 0).normalize(),
  new THREE.Vector3(-1, 1, 2).normalize(),
  new THREE.Vector3(1, -1, 1).normalize(),
)
const ISO_Q = new THREE.Quaternion().setFromRotationMatrix(ISO_ROT)
const IDENTITY = new THREE.Quaternion()

export const clampIso = (v: number | null | undefined) => (Number.isFinite(v) ? Math.max(0, Math.min(1, v as number)) : 0)

/** Camera orientation at iso t (slerp identity → ISO_ROT; camera looks along its −z like Blender's). */
export function isoQuaternion(t: number, out = new THREE.Quaternion()): THREE.Quaternion {
  return out.slerpQuaternions(IDENTITY, ISO_Q, clampIso(t))
}

/** -> (camera x, y, z axes) at iso t. z = unit direction from the target toward the camera. */
export function isoBasis(t: number): { x: THREE.Vector3; y: THREE.Vector3; z: THREE.Vector3 } {
  const q = isoQuaternion(t)
  return {
    x: new THREE.Vector3(1, 0, 0).applyQuaternion(q),
    y: new THREE.Vector3(0, 1, 0).applyQuaternion(q),
    z: new THREE.Vector3(0, 0, 1).applyQuaternion(q),
  }
}

export interface OrthoFrame {
  /** Centre of the subject's 3D bounds (the camera orbits it). */
  target: THREE.Vector3
  /** Frame centre offset in camera x / y (lens shift, world units). */
  cx: number
  cy: number
  /** Blender ortho_scale: the frame's (square) side in world units. */
  scale: number
  /** Distance from the target to every point's farthest reach (camera placement / clipping). */
  reach: number
}

/**
 * framing.ortho_plan for one still: `points` = world xyz triplets of the subject. Empty → the front framing.
 * The frame is square (Blender renders); the viewport shows it on its shorter side.
 */
export function orthoFrame(points: ArrayLike<number>, n: number, t: number, zoom = 1, margin = FRONT_MARGIN): OrthoFrame {
  const z = Math.max(0.05, zoom || 1)
  if (n <= 0) return { target: new THREE.Vector3(), cx: 0, cy: 0, scale: FRONT_ORTHO_SCALE / z, reach: 1 }
  let x0 = Infinity
  let y0 = Infinity
  let z0 = Infinity
  let x1 = -Infinity
  let y1 = -Infinity
  let z1 = -Infinity
  for (let i = 0; i < n; i++) {
    const x = points[3 * i]
    const y = points[3 * i + 1]
    const w = points[3 * i + 2]
    if (x < x0) x0 = x
    if (x > x1) x1 = x
    if (y < y0) y0 = y
    if (y > y1) y1 = y
    if (w < z0) z0 = w
    if (w > z1) z1 = w
  }
  const target = new THREE.Vector3((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)
  const { x: ax, y: ay } = isoBasis(t)
  let u0 = Infinity
  let v0 = Infinity
  let u1 = -Infinity
  let v1 = -Infinity
  let reach = 0
  for (let i = 0; i < n; i++) {
    const dx = points[3 * i] - target.x
    const dy = points[3 * i + 1] - target.y
    const dz = points[3 * i + 2] - target.z
    const u = dx * ax.x + dy * ax.y + dz * ax.z
    const v = dx * ay.x + dy * ay.y + dz * ay.z
    if (u < u0) u0 = u
    if (u > u1) u1 = u
    if (v < v0) v0 = v
    if (v > v1) v1 = v
    reach = Math.max(reach, Math.hypot(dx, dy, dz))
  }
  const half = Math.max(1e-6, (u1 - u0) / 2, (v1 - v0) / 2)
  const scale = (2 * half) / Math.max(0.05, 1 - 2 * margin) / z
  return { target, cx: (u0 + u1) / 2, cy: (v0 + v1) / 2, scale, reach }
}

/** Convex hull (Andrew's monotone chain) of flat x,y pairs -> CCW flat pairs. */
export function hull2d(pts: ArrayLike<number>): number[] {
  const P: [number, number][] = []
  for (let i = 0; i + 1 < pts.length; i += 2) P.push([pts[i], pts[i + 1]])
  P.sort((a, b) => a[0] - b[0] || a[1] - b[1])
  if (P.length <= 2) return P.flat()
  const cross = (o: number[], a: number[], b: number[]) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
  const half = (seq: [number, number][]) => {
    const out: [number, number][] = []
    for (const q of seq) {
      while (out.length >= 2 && cross(out[out.length - 2], out[out.length - 1], q) <= 1e-12) out.pop()
      out.push(q)
    }
    return out
  }
  const lower = half(P)
  const upper = half([...P].reverse())
  return [...lower.slice(0, -1), ...upper.slice(0, -1)].flat()
}

/** Area of a polygon (flat x,y pairs; positive when CCW). */
export function polyArea(p: ArrayLike<number>): number {
  let a = 0
  const n = p.length >> 1
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n
    a += p[2 * i] * p[2 * j + 1] - p[2 * j] * p[2 * i + 1]
  }
  return a / 2
}

/** Intersection area of two convex CCW polygons (Sutherland–Hodgman clipping of `a` by every edge of `b`). */
export function convexOverlapArea(a: ArrayLike<number>, b: ArrayLike<number>): number {
  let poly: number[] = Array.from(a)
  const m = b.length >> 1
  for (let e = 0; e < m && poly.length >= 6; e++) {
    const ax = b[2 * e]
    const ay = b[2 * e + 1]
    const bx = b[2 * ((e + 1) % m)]
    const by = b[2 * ((e + 1) % m) + 1]
    const side = (x: number, y: number) => (bx - ax) * (y - ay) - (by - ay) * (x - ax) // > 0: inside (left)
    const out: number[] = []
    const n = poly.length >> 1
    for (let i = 0; i < n; i++) {
      const px = poly[2 * i]
      const py = poly[2 * i + 1]
      const qx = poly[2 * ((i + 1) % n)]
      const qy = poly[2 * ((i + 1) % n) + 1]
      const sp = side(px, py)
      const sq = side(qx, qy)
      if (sp >= 0) out.push(px, py)
      if ((sp >= 0) !== (sq >= 0)) {
        const t = sp / (sp - sq)
        out.push(px + (qx - px) * t, py + (qy - py) * t)
      }
    }
    poly = out
  }
  return poly.length >= 6 ? Math.max(0, polyArea(poly)) : 0
}

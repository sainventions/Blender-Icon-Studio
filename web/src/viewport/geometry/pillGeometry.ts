// "Pill" extrusion: a closed solid whose cross-section is the contour and whose edge profile is a round bevel —
// the three.js twin of Blender's 2D curve with extrude + bevel_mode='ROUND' + offset=−bevel (PLAN D3).
//
// Why not THREE.ExtrudeGeometry: it emits non-indexed, flat-normal geometry (faceted bevels), and smoothing it
// afterwards mixes the cap and wall normals. Glass lives and dies by its edge normals, so we build the bevel
// rings ourselves with *analytic* normals: n = sinθ·n2D + cosθ·ẑ, split only at real contour corners.
//
// Layout (local, art units): back face at z = 0, front face at z = T where T = max(thickness, 2·bevel) — exactly
// Blender's extrude = max(T/2 − b, 0) + bevel_depth = b. The silhouette (widest ring) equals the input outline;
// caps are the outline inset by the bevel radius. UVs are the planar art-space projection u=(x+1)/2, v=(y+1)/2
// on every vertex (Blender: TexCoord Object → Mapping), so the layer texture lines up on caps and walls alike.
import * as THREE from 'three'
import type { Contour, ContourGroup } from './flatten'

export interface PillOptions {
  thickness: number
  /** Round bevel radius (already clamped to the layer's safe radius by the caller). 0 = sharp slab. */
  bevel: number
  /** Bevel rings per quarter circle. */
  segments: number
  /** Contour turns sharper than this keep split normals (crisp mitred corners). */
  creaseDeg?: number
  /** Added to every z (e.g. −T for the plate whose front face sits at z = 0). */
  zOffset?: number
}

interface Ring {
  inset: number
  z: number
  nr: number
  nz: number
}

interface Frames {
  n: number
  /** Outward unit normal of edge i (p_i → p_i+1). */
  en: Float64Array
  /** Miter offset vector per vertex (outward, length ≥ 1). */
  miter: Float64Array
  /** Smooth vertex normal. */
  vn: Float64Array
  corner: Uint8Array
}

function buildProfile(T: number, b: number, S: number): Ring[] {
  if (b <= 1e-6) {
    return [
      { inset: 0, z: T, nr: 1, nz: 0 },
      { inset: 0, z: 0, nr: 1, nz: 0 },
    ]
  }
  const rings: Ring[] = []
  for (let k = 0; k <= S; k++) {
    const th = (k / S) * (Math.PI / 2)
    rings.push({ inset: b * (1 - Math.sin(th)), z: T - b + b * Math.cos(th), nr: Math.sin(th), nz: Math.cos(th) })
  }
  const hasWall = T - 2 * b > 1e-6
  for (let k = hasWall ? 0 : 1; k <= S; k++) {
    const th = Math.PI / 2 + (k / S) * (Math.PI / 2)
    rings.push({ inset: b * (1 - Math.sin(th)), z: b + b * Math.cos(th), nr: Math.sin(th), nz: Math.cos(th) })
  }
  // Snap the exact cap normals (sin π = 1e-16 noise).
  rings[0].nr = 0
  rings[0].nz = 1
  rings[rings.length - 1].nr = 0
  rings[rings.length - 1].nz = -1
  return rings
}

function computeFrames(pts: Float64Array, cosCrease: number): Frames {
  const n = pts.length / 2
  const en = new Float64Array(n * 2)
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n
    let dx = pts[2 * j] - pts[2 * i]
    let dy = pts[2 * j + 1] - pts[2 * i + 1]
    const l = Math.hypot(dx, dy) || 1
    dx /= l
    dy /= l
    // Outers are CCW and holes CW, so the right-hand normal always points out of the solid.
    en[2 * i] = dy
    en[2 * i + 1] = -dx
  }
  const miter = new Float64Array(n * 2)
  const vn = new Float64Array(n * 2)
  const corner = new Uint8Array(n)
  for (let i = 0; i < n; i++) {
    const p = (i - 1 + n) % n
    const ax = en[2 * p]
    const ay = en[2 * p + 1]
    const bx = en[2 * i]
    const by = en[2 * i + 1]
    let sx = ax + bx
    let sy = ay + by
    let sl = Math.hypot(sx, sy)
    if (sl < 1e-6) {
      sx = bx
      sy = by
      sl = 1
    }
    sx /= sl
    sy /= sl
    vn[2 * i] = sx
    vn[2 * i + 1] = sy
    const d = Math.max(sx * bx + sy * by, 0.3) // limits the miter length to ~3.3× at needle corners
    miter[2 * i] = sx / d
    miter[2 * i + 1] = sy / d
    corner[i] = ax * bx + ay * by < cosCrease ? 1 : 0
  }
  return { n, en, miter, vn, corner }
}

class Builder {
  pos: number[] = []
  nor: number[] = []
  uv: number[] = []
  idx: number[] = []

  vert(x: number, y: number, z: number, nx: number, ny: number, nz: number): number {
    const l = Math.hypot(nx, ny, nz) || 1
    this.pos.push(x, y, z)
    this.nor.push(nx / l, ny / l, nz / l)
    this.uv.push((x + 1) * 0.5, (y + 1) * 0.5)
    return this.pos.length / 3 - 1
  }
}

interface ContourRings {
  frames: Frames
  /** First vertex index per ring of this contour's columns. */
  ringBase: number[]
  colIn: Int32Array
  colOut: Int32Array
  cols: number
}

function emitContour(B: Builder, c: Contour, rings: Ring[], cosCrease: number): ContourRings {
  const frames = computeFrames(c.pts, cosCrease)
  const { n, en, miter, vn, corner } = frames
  const colIn = new Int32Array(n)
  const colOut = new Int32Array(n)
  let cols = 0
  for (let i = 0; i < n; i++) {
    colIn[i] = cols++
    colOut[i] = corner[i] ? cols++ : colIn[i]
  }
  const ringBase: number[] = []
  for (const r of rings) {
    ringBase.push(B.pos.length / 3)
    for (let i = 0; i < n; i++) {
      const x = c.pts[2 * i] - miter[2 * i] * r.inset
      const y = c.pts[2 * i + 1] - miter[2 * i + 1] * r.inset
      if (corner[i]) {
        const p = (i - 1 + n) % n
        B.vert(x, y, r.z, en[2 * p] * r.nr, en[2 * p + 1] * r.nr, r.nz)
        B.vert(x, y, r.z, en[2 * i] * r.nr, en[2 * i + 1] * r.nr, r.nz)
      } else {
        B.vert(x, y, r.z, vn[2 * i] * r.nr, vn[2 * i + 1] * r.nr, r.nz)
      }
    }
  }
  // Side quads (front ring → back ring), wound so their geometric normal faces outward.
  for (let r = 0; r < rings.length - 1; r++) {
    const b0 = ringBase[r]
    const b1 = ringBase[r + 1]
    for (let i = 0; i < n; i++) {
      const j = (i + 1) % n
      const a = b0 + colOut[i]
      const b = b0 + colIn[j]
      const cc = b1 + colIn[j]
      const d = b1 + colOut[i]
      B.idx.push(a, cc, b, a, d, cc)
    }
  }
  return { frames, ringBase, colIn, colOut, cols }
}

function triangulateCap(
  B: Builder,
  group: ContourGroup,
  parts: ContourRings[],
  ringIndex: (cr: ContourRings) => number,
  inset: number,
  front: boolean,
  separate: { z: number } | null,
): void {
  const all: { c: Contour; cr: ContourRings }[] = [{ c: group.outer, cr: parts[0] }]
  group.holes.forEach((h, k) => all.push({ c: h, cr: parts[k + 1] }))
  const contourV: THREE.Vector2[] = []
  const holesV: THREE.Vector2[][] = []
  const indices: number[] = []
  all.forEach(({ c, cr }, k) => {
    const list: THREE.Vector2[] = []
    const n = c.pts.length / 2
    for (let i = 0; i < n; i++) {
      const x = c.pts[2 * i] - cr.frames.miter[2 * i] * inset
      const y = c.pts[2 * i + 1] - cr.frames.miter[2 * i + 1] * inset
      list.push(new THREE.Vector2(x, y))
      if (separate) indices.push(B.vert(x, y, separate.z, 0, 0, front ? 1 : -1))
      else indices.push(ringIndex(cr) + cr.colOut[i])
    }
    if (k === 0) contourV.push(...list)
    else holesV.push(list)
  })
  const flatPts: THREE.Vector2[] = [...contourV, ...holesV.flat()]
  let faces: number[][]
  try {
    faces = THREE.ShapeUtils.triangulateShape(contourV, holesV)
  } catch {
    return
  }
  for (const f of faces) {
    const [a, b, c] = f
    const pa = flatPts[a]
    const pb = flatPts[b]
    const pc = flatPts[c]
    if (!pa || !pb || !pc) continue
    const area = (pb.x - pa.x) * (pc.y - pa.y) - (pc.x - pa.x) * (pb.y - pa.y)
    if (Math.abs(area) < 1e-14) continue
    const ccw = area > 0
    if (ccw === front) B.idx.push(indices[a], indices[b], indices[c])
    else B.idx.push(indices[a], indices[c], indices[b])
  }
}

/** Build one indexed BufferGeometry (position, normal, uv) for a set of contour groups. */
export function buildPillGeometry(groups: ContourGroup[], o: PillOptions): THREE.BufferGeometry {
  const b = Math.max(0, o.bevel)
  const T = Math.max(o.thickness, 2 * b, 1e-4)
  const S = Math.max(1, Math.round(o.segments))
  const cosCrease = Math.cos(((o.creaseDeg ?? 30) * Math.PI) / 180)
  const rings = buildProfile(T, b, S)
  const flat = b <= 1e-6
  const B = new Builder()

  for (const g of groups) {
    const parts: ContourRings[] = [emitContour(B, g.outer, rings, cosCrease)]
    for (const h of g.holes) parts.push(emitContour(B, h, rings, cosCrease))
    const last = rings.length - 1
    if (flat) {
      triangulateCap(B, g, parts, () => 0, 0, true, { z: T })
      triangulateCap(B, g, parts, () => 0, 0, false, { z: 0 })
    } else {
      triangulateCap(B, g, parts, (cr) => cr.ringBase[0], rings[0].inset, true, null)
      triangulateCap(B, g, parts, (cr) => cr.ringBase[last], rings[last].inset, false, null)
    }
  }

  const geo = new THREE.BufferGeometry()
  const zOff = o.zOffset ?? 0
  const pos = new Float32Array(B.pos)
  if (zOff !== 0) for (let i = 2; i < pos.length; i += 3) pos[i] += zOff
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3))
  geo.setAttribute('normal', new THREE.BufferAttribute(new Float32Array(B.nor), 3))
  geo.setAttribute('uv', new THREE.BufferAttribute(new Float32Array(B.uv), 2))
  const vcount = pos.length / 3
  geo.setIndex(new THREE.BufferAttribute(vcount > 65535 ? new Uint32Array(B.idx) : new Uint16Array(B.idx), 1))
  if (vcount > 0) {
    geo.computeBoundingBox()
    geo.computeBoundingSphere()
  }
  geo.userData.thickness = T
  return geo
}

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

export interface Frames {
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

/**
 * The worker's GN route (cusps, unfilletable acute corners) bevels with the Bevel modifier's "clamp overlap": the bevel
 * never reaches past the middle of the body. The pill insets every vertex by miter × bevel, which on a sliver thinner
 * than twice the bevel (a fold's tip, a spike) crossed the opposite side and stuck out of the outline as a spike.
 * Each vertex's inset is limited to half the distance to the nearest other edge of the group along its inset ray
 * (the miter vector is scaled in place, so rings and caps follow).
 */
export function clampInsets(contours: Contour[], frames: Frames[], bevel: number): void {
  if (bevel <= 1e-6) return
  const ax: number[] = []
  const ay: number[] = []
  const bx: number[] = []
  const by: number[] = []
  const owner: number[] = [] // contour index
  const local: number[] = [] // edge index within its contour (edge i: p_i → p_i+1)
  contours.forEach((c, ci) => {
    const n = c.pts.length / 2
    for (let i = 0; i < n; i++) {
      const j = (i + 1) % n
      ax.push(c.pts[2 * i])
      ay.push(c.pts[2 * i + 1])
      bx.push(c.pts[2 * j])
      by.push(c.pts[2 * j + 1])
      owner.push(ci)
      local.push(i)
    }
  })
  const reach = 2 * (bevel / 0.3) // ray length: twice the longest possible miter offset
  const h = Math.max(reach, 1e-4)
  const grid = new Map<number, number[]>()
  const key = (ix: number, iy: number) => (ix + 32768) * 65536 + (iy + 32768)
  for (let e = 0; e < ax.length; e++) {
    const x0 = Math.floor(Math.min(ax[e], bx[e]) / h)
    const x1 = Math.floor(Math.max(ax[e], bx[e]) / h)
    const y0 = Math.floor(Math.min(ay[e], by[e]) / h)
    const y1 = Math.floor(Math.max(ay[e], by[e]) / h)
    for (let ix = x0; ix <= x1; ix++)
      for (let iy = y0; iy <= y1; iy++) {
        const k = key(ix, iy)
        const list = grid.get(k)
        if (list) list.push(e)
        else grid.set(k, [e])
      }
  }
  contours.forEach((c, ci) => {
    const f = frames[ci]
    const n = f.n
    for (let i = 0; i < n; i++) {
      const mx = f.miter[2 * i]
      const my = f.miter[2 * i + 1]
      const ml = Math.hypot(mx, my)
      if (ml < 1e-9) continue
      const D = ml * bevel
      const ux = -mx / ml
      const uy = -my / ml
      const px = c.pts[2 * i]
      const py = c.pts[2 * i + 1]
      const qx = px + ux * 2 * D
      const qy = py + uy * 2 * D
      let tmin = Infinity
      const prev = (i - 1 + n) % n
      for (let ix = Math.floor(Math.min(px, qx) / h); ix <= Math.floor(Math.max(px, qx) / h); ix++) {
        for (let iy = Math.floor(Math.min(py, qy) / h); iy <= Math.floor(Math.max(py, qy) / h); iy++) {
          const list = grid.get(key(ix, iy))
          if (!list) continue
          for (const e of list) {
            if (owner[e] === ci && (local[e] === i || local[e] === prev)) continue
            // P + t·u = A + s·(B − A)
            const dx = bx[e] - ax[e]
            const dy = by[e] - ay[e]
            const den = ux * dy - uy * dx
            if (Math.abs(den) < 1e-14) continue
            const wx = ax[e] - px
            const wy = ay[e] - py
            const t = (wx * dy - wy * dx) / den
            const sp = (wx * uy - wy * ux) / den
            if (t > 1e-9 && sp >= 0 && sp <= 1 && t < tmin) tmin = t
          }
        }
      }
      if (tmin < 2 * D) {
        const k = Math.max(0.02, (0.5 * tmin) / D)
        f.miter[2 * i] *= k
        f.miter[2 * i + 1] *= k
      }
    }
  })
}

function emitContour(B: Builder, c: Contour, frames: Frames, rings: Ring[]): ContourRings {
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

/**
 * The cap contour is the outline inset by the bevel radius. Where a local feature of the outline is smaller than the
 * inset (a short chamfer between two corner fillets, a tight wiggle of a traced contour) the mitred offset folds back
 * into a tiny self-intersecting loop, and earcut then mis-triangulates the WHOLE cap — holes filled, triangles across
 * them (Sheets' grid rendered as wedges). Edges whose direction reversed against the outline are collapsed (their end
 * points merged at the midpoint) until none is left. Mutates `pts`; returns the indices of the kept points and, for
 * every input point, the kept point it was merged into (`rep`).
 */
export function untangleInset(outline: ArrayLike<number>, pts: THREE.Vector2[]): { keep: number[]; rep: number[] } {
  const keep = pts.map((_, i) => i)
  const rep = pts.map((_, i) => i)
  let k = 0
  let guard = 4 * pts.length + 8
  while (k < keep.length && keep.length > 3 && guard-- > 0) {
    const i = keep[k]
    const kj = (k + 1) % keep.length
    const j = keep[kj]
    const ox = outline[2 * j] - outline[2 * i]
    const oy = outline[2 * j + 1] - outline[2 * i + 1]
    const ex = pts[j].x - pts[i].x
    const ey = pts[j].y - pts[i].y
    if (ox * ex + oy * ey < 0) {
      pts[i].set((pts[i].x + pts[j].x) / 2, (pts[i].y + pts[j].y) / 2)
      keep.splice(kj, 1)
      for (let q = 0; q < rep.length; q++) if (rep[q] === j) rep[q] = i
      if (kj < k) k-- // removed the first entry: everything shifted down by one
      k = Math.max(0, k - 1) // the merged point changes the previous edge too
    } else {
      k++
    }
  }
  return { keep, rep }
}

function triangulateCap(
  B: Builder,
  group: ContourGroup,
  parts: ContourRings[],
  ringIndex: (cr: ContourRings) => number,
  inset: number,
  front: boolean,
  separate: { z: number } | null,
  allowFallback = true,
): boolean {
  const all: { c: Contour; cr: ContourRings }[] = [{ c: group.outer, cr: parts[0] }]
  group.holes.forEach((h, k) => all.push({ c: h, cr: parts[k + 1] }))
  const contourV: THREE.Vector2[] = []
  const holesV: THREE.Vector2[][] = []
  // The same points on the (always simple) outline: fallback triangulation when the inset contour still crosses itself.
  const contourO: THREE.Vector2[] = []
  const holesO: THREE.Vector2[][] = []
  const indices: number[] = []
  all.forEach(({ c, cr }, k) => {
    const raw: THREE.Vector2[] = []
    const n = c.pts.length / 2
    for (let i = 0; i < n; i++) {
      raw.push(
        new THREE.Vector2(c.pts[2 * i] - cr.frames.miter[2 * i] * inset, c.pts[2 * i + 1] - cr.frames.miter[2 * i + 1] * inset),
      )
    }
    const { keep, rep } = inset > 0 ? untangleInset(c.pts, raw) : { keep: raw.map((_, i) => i), rep: null }
    if (rep && !separate) {
      // Collapse the loop in the shared cap ring too (wall top edge), so the cap triangles keep their orientation.
      const base = ringIndex(cr)
      for (let i = 0; i < n; i++) {
        const v = raw[rep[i]]
        for (const col of [cr.colIn[i], cr.colOut[i]]) {
          B.pos[3 * (base + col)] = v.x
          B.pos[3 * (base + col) + 1] = v.y
          B.uv[2 * (base + col)] = (v.x + 1) * 0.5
          B.uv[2 * (base + col) + 1] = (v.y + 1) * 0.5
        }
      }
    }
    const list: THREE.Vector2[] = []
    const olist: THREE.Vector2[] = []
    for (const i of keep) {
      const v = raw[i]
      list.push(v)
      olist.push(new THREE.Vector2(c.pts[2 * i], c.pts[2 * i + 1]))
      if (separate) indices.push(B.vert(v.x, v.y, separate.z, 0, 0, front ? 1 : -1))
      else indices.push(ringIndex(cr) + cr.colOut[i])
    }
    if (list.length < 3) {
      indices.length -= list.length
      return
    }
    if (k === 0) {
      contourV.push(...list)
      contourO.push(...olist)
    } else {
      holesV.push(list)
      holesO.push(olist)
    }
  })
  if (contourV.length < 3) return true
  let flatPts: THREE.Vector2[] = [...contourV, ...holesV.flat()]
  const expected = Math.abs(THREE.ShapeUtils.area(contourV)) - holesV.reduce((acc, h) => acc + Math.abs(THREE.ShapeUtils.area(h)), 0)
  let faces: number[][]
  try {
    faces = THREE.ShapeUtils.triangulateShape(contourV, holesV)
  } catch {
    return false
  }
  let covered = 0
  for (const [a, b, c] of faces) {
    const pa = flatPts[a]
    const pb = flatPts[b]
    const pc = flatPts[c]
    if (pa && pb && pc) covered += Math.abs((pb.x - pa.x) * (pc.y - pa.y) - (pc.x - pa.x) * (pb.y - pa.y)) / 2
  }
  const consistent = covered <= Math.max(expected, 0) * 1.01 + 1e-9
  if (!consistent && !allowFallback) return false
  if (!consistent && contourO.length === contourV.length) {
    // The inset contour still crosses itself (a fold the clamp / untangle could not resolve): earcut overlapped
    // triangles. Use the connectivity of the outline (a simple polygon; the inset is a deformation of it).
    try {
      const of = THREE.ShapeUtils.triangulateShape(contourO, holesO)
      faces = of
      flatPts = [...contourO, ...holesO.flat()]
    } catch {
      // keep the inset triangulation
    }
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
  return consistent
}

/** Halvings of the bevel tried for a piece whose inset caps fold (see buildPillGeometry). */
export const MAX_BEVEL_RETRIES = 3

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
    const contours = [g.outer, ...g.holes]
    if (flat) {
      const frames = contours.map((c) => computeFrames(c.pts, cosCrease))
      const parts: ContourRings[] = contours.map((c, k) => emitContour(B, c, frames[k], rings))
      triangulateCap(B, g, parts, () => 0, 0, true, { z: T })
      triangulateCap(B, g, parts, () => 0, 0, false, { z: 0 })
      continue
    }
    // A piece whose inset caps still fold (features thinner than the bevel that the per-vertex clamp cannot resolve)
    // is rebuilt with half the bevel, like the worker's last-resort bevel clamped to the piece's own safe radius.
    let gb = b
    for (let attempt = 0; ; attempt++) {
      const mark = [B.pos.length, B.nor.length, B.uv.length, B.idx.length]
      const gr = gb === b ? rings : buildProfile(T, gb, S)
      const frames = contours.map((c) => computeFrames(c.pts, cosCrease))
      clampInsets(contours, frames, gb)
      const parts: ContourRings[] = contours.map((c, k) => emitContour(B, c, frames[k], gr))
      const last = gr.length - 1
      const final = attempt >= MAX_BEVEL_RETRIES
      const okFront = triangulateCap(B, g, parts, (cr) => cr.ringBase[0], gr[0].inset, true, null, final)
      const okBack = okFront && triangulateCap(B, g, parts, (cr) => cr.ringBase[last], gr[last].inset, false, null, final)
      if ((okFront && okBack) || final) break
      B.pos.length = mark[0]
      B.nor.length = mark[1]
      B.uv.length = mark[2]
      B.idx.length = mark[3]
      gb *= 0.5
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

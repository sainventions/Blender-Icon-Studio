// Height-field bodies of the live view (PLAN §11 Geometry): the port of blender_worker/heightfield.py with poly2tri
// (viewport/geometry/heightfield.ts) and the layer / plate body layout (viewport/geometry/layerGeometry.ts).
// Run (from web/): node --test src/viewport/tests/
import { fixture, read, pyConst } from './helpers.mjs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const hf = await import('../geometry/heightfield.ts')
const lg = await import('../geometry/layerGeometry.ts')

const near = (a, b, eps, msg = '') => assert.ok(Math.abs(a - b) <= eps, `${msg} ${a} ≉ ${b} (±${eps})`)
const poly = (pts) => ({ closed: true, hole: false, parent: -1, depth: 0, points: pts.map((p) => ({ co: p, hl: p, hr: p })) })
/** Circle of radius r as 4 cubic béziers. */
function circle(r, cx = 0, cy = 0, hole = false) {
  const k = 0.5522847498 * r
  const pts = [
    { co: [cx + r, cy], hl: [cx + r, cy - k], hr: [cx + r, cy + k] },
    { co: [cx, cy + r], hl: [cx + k, cy + r], hr: [cx - k, cy + r] },
    { co: [cx - r, cy], hl: [cx - r, cy + k], hr: [cx - r, cy - k] },
    { co: [cx, cy - r], hl: [cx - k, cy - r], hr: [cx + k, cy - r] },
  ]
  return { closed: true, hole, parent: hole ? 0 : -1, depth: hole ? 1 : 0, points: pts } // organise() orients
}
function build(splines, t, b, k = 0, seg = 6, scale = 1) {
  const box = {}
  const arr = hf.buildBody(splines, t, b, k, seg, scale, box)
  assert.ok(arr, 'body built')
  return { arr, mesh: box.out }
}
function assertClean(mesh, label) {
  const c = hf.checkBody(mesh)
  assert.equal(c.nonManifold, 0, `${label}: non-manifold edges`)
  assert.equal(c.misoriented, 0, `${label}: mis-wound edges`)
  assert.equal(c.invertedNormals, 0, `${label}: normals facing away from their faces`)
  assert.equal(c.selfIntersections, 0, `${label}: self-intersections`)
  assert.ok(c.volume > 0, `${label}: volume ${c.volume}`)
  return c
}
/** Top vertices (z ≥ 0 copies) as [x, y, z]. */
function topVerts(mesh) {
  const out = []
  for (let i = 0; i < mesh.verts.length; i += 3) if (mesh.verts[i + 2] >= 0) out.push([mesh.verts[i], mesh.verts[i + 1], mesh.verts[i + 2]])
  return out
}

// ------------------------------------------------------------------------------------------------ mirrored constants
test('sampling / profile constants mirror blender_worker/heightfield.py', () => {
  const src = read('blender_worker/heightfield.py')
  for (const k of [
    'CHORD_TOL', 'MAX_EDGE', 'MERGE_EPS', 'MERGE_Q', 'MIN_AREA', 'CORNER_DEG', 'GUARD_MIN', 'GUARD_MAX', 'FAN_DEG', 'RING_TOL',
    'KAPPA', 'KAPPA_REL', 'SOFT_REACH', 'NEAR', 'TAN_K', 'TAN_MIN', 'TAN_MAX', 'NORMAL_MIN_DOT', 'APEX_CLEAR', 'MIN_THICKNESS',
    'CHORD_PASSES', 'WALL_MIN', 'WIDTH_BISECT', 'WIDTH_TOL', 'WIDTH_BLEND', 'APEX_TIE',
  ])
    assert.equal(hf[k], pyConst(src, k), k)
  // the layer layout mirrors scene.py
  const scene = read('blender_worker/scene.py')
  for (const k of ['LAYER_EPS', 'REGION_DZ', 'CARD_THICKNESS']) assert.equal(lg[k], pyConst(scene, k), k)
})

test('profile = PLAN §11: z(d) = e + hb(d) + inflate·D·√(1 − (1 − min(d/D, 1))²), round edge of radius b', () => {
  const t = 0.1
  const b = 0.03
  const D = 0.4
  const e = t / 2 - b
  for (const k of [0, 0.5, 1])
    for (const d of [0, 0.005, 0.015, 0.03, 0.1, 0.4, 0.7]) {
      const hb = Math.sqrt(b * b - (b - Math.min(d, b)) ** 2)
      const dome = k * D * Math.sqrt(1 - (1 - Math.min(d / D, 1)) ** 2)
      near(hf.profile(d, t, b, k, D), e + hb + dome, 1e-12, `d=${d} k=${k}`)
    }
  assert.equal(hf.slope(0, b, 0, D), Infinity) // vertical tangent at the rim: smooth with the wall
  assert.equal(hf.slope(0.05, b, 0, D), 0) // flat cap beyond the round edge
  near(hf.halfHeight(t, b, 0, D), t / 2, 1e-12)
  near(hf.halfHeight(t, b, 1, D), t / 2 + D, 1e-12)
  // thin island (D < b): it tapers — half height √(b² − (b − D)²) + e < t / 2
  assert.ok(hf.halfHeight(t, b, 0, 0.01) < t / 2)
  // graded rings: uniform in angle along the quarter circle, dome rings interleaved and thinned
  const K = 6
  const bevelRings = Array.from({ length: K }, (_, j) => b * (1 - Math.cos(((j + 1) * Math.PI) / (2 * K))))
  hf.ringDistances(b, 0, D, K).forEach((v, j) => near(v, bevelRings[j], 1e-15, `ring ${j}`))
  const both = hf.ringDistances(b, 1, D, K)
  assert.ok(both.every((v, i) => i === 0 || v > both[i - 1]) && both[both.length - 1] < D)
  assert.ok(both.length <= K + 5)
})

// ------------------------------------------------------------------------------------------------ bodies
test('Gemini star: watertight, no self-intersection, tips taper instead of folding (the 6.webp breakage)', () => {
  const fx = fixture('gemini-star.json')
  const S = fx.artScale
  const t = fx.depth.thickness / S
  for (const [b, k] of [
    [Math.min(fx.depth.bevel, fx.depth.thickness / 2) / S, 0],
    [t / 2, 0], // full round edge
    [Math.min(fx.depth.bevel, fx.depth.thickness / 2) / S, 0.3], // 6.webp: inflate 0.3
  ]) {
    const { arr, mesh } = build(fx.regions[0].splines, t, b, k, fx.depth.bevelSegments, S)
    assertClean(mesh, `b=${b.toFixed(3)} k=${k}`)
    assert.equal(arr.info.fallbacks, 0)
    near(arr.info.half, hf.halfHeight(t, b, k, arr.info.D), 0.02 * t, 'half height reached at the apex')
    // the four tips (extreme x / y): every top vertex within 0.01 of a tip sits well below the half height
    const top = topVerts(mesh)
    const ext = [
      top.reduce((a, v) => (v[0] > a[0] ? v : a)),
      top.reduce((a, v) => (v[0] < a[0] ? v : a)),
      top.reduce((a, v) => (v[1] > a[1] ? v : a)),
      top.reduce((a, v) => (v[1] < a[1] ? v : a)),
    ]
    for (const tip of ext) {
      const zs = top.filter((v) => Math.hypot(v[0] - tip[0], v[1] - tip[1]) < 0.01).map((v) => v[2])
      assert.ok(Math.max(...zs) < 0.9 * arr.info.half, `tip ${tip.map((v) => v.toFixed(2))}: ${Math.max(...zs)}`)
    }
  }
})

test('sphere head: bevel = radius rounds a thin island into a bead (1.webp), lens body stays watertight', () => {
  const fx = fixture('contacts.json')
  const S = fx.artScale
  const t = 0.42 / S
  const b = 0.21 / S
  // the worker's numbers (blender_worker.heightfield.build, round 8): a 15 % wall, the round edge capped locally
  const REF = [
    { half: 0.195721, volume: 0.0867705 },
    { half: 0.195721, volume: 0.0353771 },
  ]
  fx.regions.forEach((r, i) => {
    const { arr, mesh } = build(r.splines, t, b, 0, 8, S)
    const c = assertClean(mesh, r.elementId)
    // round 8: no knife-thin rim — a WALL_MIN share of the half thickness stays a vertical wall
    near(arr.info.wall, hf.WALL_MIN * (t / 2), 1e-12, 'minimum wall')
    near(arr.info.bevel, (1 - hf.WALL_MIN) * (t / 2), 1e-12, 'rim radius')
    // the island's apex: e + min(b, D) (a thinner island is a sphere of its own radius)
    near(arr.info.half, hf.halfHeight(t, b, 0, arr.info.D), 0.01 * t, `${r.elementId} bead`)
    near(arr.info.half, REF[i].half, 2e-4 * REF[i].half, `${r.elementId} half = worker`)
    near(c.volume, REF[i].volume, 2e-3 * REF[i].volume, `${r.elementId} volume = worker`)
  })
})

test('round 8 profile: minimum wall, bevel = radius = t/2 is a near-sphere, the round edge capped at the local half-width', () => {
  // bevel = radius = half the thickness: a disc of radius R becomes a near-sphere — a round edge of radius
  // (1 − WALL_MIN)·R on the minimum wall WALL_MIN·R
  const R = 0.2
  const bb = (1 - hf.WALL_MIN) * R
  near(hf.rimBevel(2 * R, R), bb, 1e-15)
  near(hf.wallHalf(2 * R, R), hf.WALL_MIN * R, 1e-15)
  for (let i = 0; i <= 8; i++) {
    const r = (R * i) / 8
    near(hf.profile(R - r, 2 * R, R, 0, R), hf.WALL_MIN * R + Math.sqrt(bb * bb - (bb - Math.min(R - r, bb)) ** 2), 1e-12, `r=${r}`)
  }
  near(hf.profile(R, 2 * R, R, 0, R), R, 1e-12)
  // the disc model: an island narrower than the bevel is a round body of radius D, flat on its spine / apex
  const t = 0.16
  const b = 0.06
  const e = hf.wallHalf(t, b)
  for (const D of [0.01, 0.03, 0.059, 0.2]) {
    near(hf.halfHeight(t, b, 0, D), e + Math.min(b, D), 1e-12, `D=${D}`)
    near(hf.slope(D, b, 0, D), 0, 1e-9, 'flat on the spine')
  }
  for (let i = 0; i <= 4; i++) {
    const d = 0.005 * i
    near(hf.rimHeight(d, 0.02), Math.sqrt(0.02 ** 2 - (0.02 - d) ** 2), 1e-15, 'a tube of radius w')
  }
  assert.equal(hf.rimSlopes(0, 0.02)[0], Infinity)
  assert.deepEqual(hf.rimSlopes(0.02, 0.02), [0, 1])
  assert.deepEqual(hf.rimSlopes(0.03, 0.02), [0, 1])
})

/** Outline + island inradius at the body tolerances of bevel b (worker test helper _outline). */
function outlineOf(splines, b = 0.04) {
  const o = hf.outline(splines, hf.CHORD_TOL, hf.MAX_EDGE, hf.MERGE_EPS, Math.min(Math.max(0.5 * b, hf.GUARD_MIN), hf.GUARD_MAX))
  const ol = new hf.OutlineQuery(o, Math.max(hf.NEAR, 16 * hf.CHORD_TOL))
  return { ol, o, D: hf.islandInradius(ol) }
}
/** worker geometry.rect_spline: a rectangle with handles at 1/3. */
function rect(x0, y0, x1, y1) {
  const c = [
    [x1, y1],
    [x0, y1],
    [x0, y0],
    [x1, y0],
  ]
  return {
    closed: true,
    hole: false,
    parent: -1,
    depth: 0,
    points: c.map(([x, y], i) => {
      const [px, py] = c[(i + 3) % 4]
      const [nx, ny] = c[(i + 1) % 4]
      return { co: [x, y], hl: [x + (px - x) / 3, y + (py - y) / 3], hr: [x + (nx - x) / 3, y + (ny - y) / 3] }
    }),
  }
}
const ringVerts = (o) => o.rings.flatMap((r) => Array.from({ length: r.length >> 1 }, (_, i) => [r[2 * i], r[2 * i + 1]]))

test('local half-width (worker local_width / blend_width): strips, discs, wide corners; round tubes without a ridge', () => {
  const strip = poly([
    [-0.5, -0.03],
    [0.5, -0.03],
    [0.5, 0.03],
    [-0.5, 0.03],
  ])
  let { ol, o, D } = outlineOf([strip], 0.08)
  let w = hf.localWidth(ol, D, 2 * hf.CHORD_TOL)
  ringVerts(o).forEach(([x], i) => {
    if (Math.abs(x) < 0.4) near(w[i], 0.03, 0.0015, `strip side x=${x}`)
  })
  ;({ ol, o, D } = outlineOf([circle(0.05)], 0.08))
  for (const v of hf.localWidth(ol, D, 2 * hf.CHORD_TOL)) near(v, 0.05, 0.003, 'disc radius')
  ;({ ol, o, D } = outlineOf([rect(-0.4, -0.4, 0.4, 0.4)], 0.08))
  const raw = hf.localWidth(ol, D, 2 * hf.CHORD_TOL)
  const win = hf.localWidth(ol, D, 2 * hf.CHORD_TOL, 0.08)
  const corner = ringVerts(o).map(([x, y]) => Math.hypot(Math.abs(x) - 0.4, Math.abs(y) - 0.4) < 1e-9)
  assert.equal(corner.filter(Boolean).length, 4)
  assert.ok(Math.max(...raw.filter((_, i) => corner[i])) < 0.01, 'the corner itself → 0')
  assert.ok(Math.min(...win.filter((_, i) => corner[i])) > 0.06, 'windowed: a wide part keeps its round edge')
  // blend_width on the strip: the spine gets the half width from both sides, beside a side its own foot's value
  ;({ ol, o, D } = outlineOf([strip], 0.08))
  w = hf.localWidth(ol, D, 2 * hf.CHORD_TOL, 0.08)
  const bw = hf.blendWidth(ol, w, [0, 0, 0.1, 0.02, 0.1, -0.025])
  bw.w.forEach((v) => near(v, 0.03, 0.002, 'blended width'))
  ;[0.03, 0.01, 0.005].forEach((v, i) => near(bw.d[i], v, 1e-6, 'distance'))
  assert.ok(Math.max(...Array.from(bw.gw, Math.abs)) < 0.05, 'constant width: no slope')
  // a thin stroke with a big bevel is a ROUND TUBE of its own radius: no roof ridge on the spine
  const { arr, mesh } = build([strip], 0.16, 0.08, 0, 8)
  assertClean(mesh, 'tube')
  const e = hf.wallHalf(0.16, 0.08)
  near(arr.info.half, e + 0.03, 0.0015, 'tube top = wall + half width')
  for (const [x, y, z] of topVerts(mesh))
    if (Math.abs(x) < 0.3 && Math.abs(y) < 0.027) near(z, e + Math.sqrt(0.03 ** 2 - y * y), 0.0025, `tube cross-section at y=${y.toFixed(4)}`)
})

test('deterministic dome apex (worker island_inradius): tied cells / moves break the same way on both sides', () => {
  const c = new Map()
  hf.islandInradius(outlineOf([rect(-0.3, -0.1, 0.5, 0.1)], 0.04).ol, 40, c)
  near(c.get(0)[0], 0.1, 0.02, 'apex x = the rectangle centre')
  near(c.get(0)[1], 0, 0.01, 'apex y')
  const m = new Map()
  hf.islandInradius(outlineOf([rect(-0.5, -0.1, 0.3, 0.1)], 0.04).ol, 40, m)
  near(m.get(0)[0], -c.get(0)[0], 0.02, 'the mirror image gives the mirrored apex')
})

test('inflated disc reaches its apex (island centre Steiner point), dome normals are radial', () => {
  const { arr, mesh } = build([circle(0.5)], 0.1, 0.03, 1, 6)
  assertClean(mesh, 'dome')
  near(arr.info.D, 0.5, 0.005)
  near(arr.info.half, hf.halfHeight(0.1, 0.03, 1, 0.5), 0.003)
  const c = hf.checkBody(mesh)
  assert.ok(c.minNormalDot > 0.5, `normals face their faces (${c.minNormalDot})`)
})

test('flat slab (bevel 0, inflate 0): sharp rim and creased wall corners', () => {
  const { arr, mesh } = build([poly([[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5]])], 0.1, 0, 0, 6)
  assertClean(mesh, 'slab')
  near(arr.info.half, 0.05, 1e-12)
  // every wall corner normal is its quad's own (horizontal, axis-aligned) normal
  for (let q = 0; q < mesh.quads.length / 4; q++)
    for (let c = 0; c < 4; c++) {
      const n = mesh.quadNormals.subarray(12 * q + 3 * c, 12 * q + 3 * c + 3)
      near(Math.max(Math.abs(n[0]), Math.abs(n[1])), 1, 1e-9, 'axis-aligned wall normal')
      near(n[2], 0, 1e-12)
    }
})

test('holes and islands: a ring with a hole plus a separate disc, one triangulation each', () => {
  const { arr, mesh } = build([circle(0.6), circle(0.3, 0, 0, true), circle(0.15, 0.9, 0.9)], 0.1, 0.04, 0, 6)
  assertClean(mesh, 'donut + disc')
  assert.equal(arr.info.holes, 1)
  assert.equal(arr.info.islands, 2)
  // nothing is built inside the hole
  assert.ok(topVerts(mesh).every((v) => Math.hypot(v[0], v[1]) > 0.3 - 1e-3))
})

test('poly2tri: collinear constraint points (subdivided straight runs) triangulate without a fallback; the CDT is Delaunay', () => {
  const sq = poly([[-1, -1], [1, -1], [1, 1], [-1, 1]])
  const { arr, mesh } = build([sq], 0.16, 0.04, 0, 8)
  assert.equal(arr.info.fallbacks, 0)
  assertClean(mesh, 'square plate')
  // Delaunay: every unconstrained interior edge has an empty circumcircle (no rim normal smeared across the cap)
  const sc = 1
  const o = hf.outline([sq], hf.CHORD_TOL / sc, hf.MAX_EDGE / sc, hf.MERGE_EPS / sc, 0.02)
  const ol = new hf.OutlineQuery(o, hf.NEAR)
  const centres = new Map()
  const Dr = hf.islandInradius(ol, 40, centres)
  const st = hf.mergePoints(hf.steinerPoints(ol, Dr, 0.04, 0, 8, hf.MAX_EDGE, hf.TAN_MIN, 1), hf.MERGE_Q, Array.from(o.rings[0]))
  const { V, T, nOutline } = hf.triangulate(ol, st)
  const edge = new Map()
  for (let i = 0; i < T.length; i += 3) for (let c = 0; c < 3; c++) edge.set(`${T[i + c]}>${T[i + ((c + 1) % 3)]}`, i)
  let bad = 0
  for (let i = 0; i < T.length; i += 3)
    for (let c = 0; c < 3; c++) {
      const a = T[i + c]
      const b = T[i + ((c + 1) % 3)]
      const cc = T[i + ((c + 2) % 3)]
      const j = edge.get(`${b}>${a}`)
      if (j === undefined || (a < nOutline && b < nOutline && (Math.abs(a - b) === 1 || Math.abs(a - b) === nOutline - 1))) continue
      const d = [T[j], T[j + 1], T[j + 2]].find((v) => v !== a && v !== b)
      const P = (v) => [V[2 * v] - V[2 * d], V[2 * v + 1] - V[2 * d + 1]]
      const [A, B, C] = [P(a), P(b), P(cc)]
      const det =
        (A[0] ** 2 + A[1] ** 2) * (B[0] * C[1] - C[0] * B[1]) -
        (B[0] ** 2 + B[1] ** 2) * (A[0] * C[1] - C[0] * A[1]) +
        (C[0] ** 2 + C[1] ** 2) * (A[0] * B[1] - B[0] * A[1])
      if (det > 1e-12) bad++
    }
  assert.equal(bad, 0)
})

test('Earth wave hairpins: the body stays inside its outline and watertight', () => {
  const fx = fixture('earth-wave.json')
  const S = 1.072959
  const { arr, mesh } = build([fx.spline], 0.1 / S, 0.045 / S, 0, 6, S)
  assertClean(mesh, 'earth wave')
  assert.equal(arr.info.fallbacks, 0)
  const ring = hf.flattenSpline(fx.spline, 1e-4, 0)
  const inside = (x, y) => {
    let c = false
    const n = ring.length / 2
    for (let i = 0, j = n - 1; i < n; j = i++)
      if (ring[2 * i + 1] > y !== ring[2 * j + 1] > y && x < ((ring[2 * j] - ring[2 * i]) * (y - ring[2 * i + 1])) / (ring[2 * j + 1] - ring[2 * i + 1]) + ring[2 * i]) c = !c
    return c
  }
  // triangle centroids of the top lie inside the outline (no wedge across the gap between the waves)
  let out = 0
  for (let t = 0; t < mesh.tris.length / 6; t++) {
    const v = [0, 1, 2].map((c) => mesh.tris[3 * t + c])
    const x = v.reduce((s, k) => s + mesh.verts[3 * k], 0) / 3
    const y = v.reduce((s, k) => s + mesh.verts[3 * k + 1], 0) / 3
    if (!inside(x, y)) out++
  }
  assert.ok(out <= 2, `${out} top triangles outside the outline`)
})

test('the intersection check catches a folded body (so "0 self-intersections" means something)', () => {
  const V = new Float64Array([0, 0, 0, 1, 0, 0, 0, 1, 0, 0.2, 0.2, -0.5, 0.3, 0.2, 0.5, 0.2, 0.3, 0.5])
  const mesh = { verts: V, tris: new Int32Array([0, 1, 2, 3, 4, 5]), triNormals: new Float64Array(18), quads: new Int32Array(0), quadNormals: new Float64Array(0) }
  assert.equal(hf.checkBody(mesh).selfIntersections, 1)
})

// ------------------------------------------------------------------------------------------------ layer layout
function layerOf(over = {}) {
  return {
    id: 'L1',
    name: 'L1',
    elementIds: ['a', 'b'],
    visible: true,
    locked: false,
    mode: 'individual',
    fill: { type: 'auto' },
    opacity: 1,
    blendMode: 'normal',
    glass: true,
    transform: { x: 0, y: 0, scale: 1 },
    depth: { z: 0.13, thickness: 0.1, bevel: 0.045, bevelSegments: 6, inflate: 0 },
    material: { preset: 'liquid_glass', params: {} },
    shadow: { kind: 'physical', opacity: 0.5 },
    ...over,
  }
}
const sq = (x0, y0, x1, y1) => poly([[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
const solid = { type: 'solid', color: '#ff0000', opacity: 1 }
function geometryOf(over = {}) {
  return {
    layerId: 'L1',
    hash: 'h1',
    silhouette: [sq(-0.5, -0.5, 0.5, 0.5)],
    regions: [
      { elementId: 'a', paint: solid, opacity: 1, zSub: 0, splines: [sq(-0.5, -0.5, 0, 0.5)] },
      { elementId: 'b', paint: solid, opacity: 1, zSub: 0, splines: [sq(0, -0.5, 0.5, 0.5)] },
    ],
    safeRadius: 0.25,
    bbox: [-0.5, -0.5, 0.5, 0.5],
    texture: '',
    texturePath: '',
    svg: '',
    images: [],
    ...over,
  }
}

test('worker layout: bevel = min(bevel, t/2) (no safe-radius clamp), touching opaque pieces merge unless a shape has its own material', () => {
  const L = layerOf()
  const g = geometryOf()
  const d = lg.layerDepth(L, 2)
  near(d.thickness, 0.05, 1e-12)
  near(d.bevel, 0.045 / 2, 1e-12)
  assert.ok(lg.touchingOpaque(g))
  assert.deepEqual(
    lg.layerBodyParts(L, g, d).map((p) => p.elementId),
    ['body'],
  )
  const own = layerOf({ elementMaterials: { b: { preset: 'chrome', params: {} } } })
  assert.deepEqual(
    lg.layerBodyParts(own, g, lg.layerDepth(own, 1)).map((p) => p.elementId),
    ['a', 'b'],
  )
  // ... but 'combined' mode is always one body
  assert.equal(lg.layerBodyParts({ ...own, mode: 'combined' }, g, d).length, 1)
  // sub-layer offsets: zSub ≥ 1 is an index × REGION_DZ; mid-plane at (t/2 + zSub) / S
  const g2 = geometryOf({ regions: [{ ...g.regions[0], zSub: 2 }, { ...g.regions[1], opacity: 0.5 }] })
  const parts = lg.layerBodyParts(L, g2, lg.layerDepth(L, 1))
  near(parts[0].z, 0.05 + 2 * lg.REGION_DZ, 1e-12)
  near(parts[1].z, 0.05, 1e-12)
  assert.equal(parts[1].opacity, 0.5)
})

test('raster images: extruded contours keep their own pixels + placement, unextruded ones become flat cards', () => {
  const L = layerOf()
  const im = { elementId: 'img', path: 'x.png', url: '/x.png', bbox: [-0.2, -0.2, 0.2, 0.2], opacity: 0.8, matrix: [0.004, 0, 0, -0.004, -0.2, 0.2], width: 100, height: 100 }
  const g = geometryOf({ images: [im, { ...im, elementId: 'a' }] })
  const parts = lg.layerBodyParts(L, g, lg.layerDepth(L, 1))
  const a = parts.find((p) => p.elementId === 'a')
  const card = parts.find((p) => p.elementId === 'img')
  assert.ok(a.image && !a.card)
  assert.ok(card.card && card.image === im)
  near(card.z, lg.CARD_THICKNESS / 2 + 0.0005, 1e-12)
  // image placement: pixel (0, 0) (top-left) → art (−0.2, 0.2) → uv (0, 1)
  const uv = lg.imageUv(im)
  near(uv[0] * -0.2 + uv[1] * 0.2 + uv[2], 0, 1e-12)
  near(uv[3] * -0.2 + uv[4] * 0.2 + uv[5], 1, 1e-12)
  const quad = lg.imageQuad(im)
  assert.equal(quad.length, 4)
  const built = card.build()
  assert.ok(built.geometry.attributes.position.count > 0)
  near(built.half, lg.CARD_THICKNESS / 2, 1e-9)
})

test('inflated layers are lifted so their lowest point stays on z; the body height feeds the iso framing', () => {
  const L = layerOf({ depth: { z: 0.13, thickness: 0.1, bevel: 0.03, bevelSegments: 6, inflate: 1 } })
  const g = geometryOf({ regions: [geometryOf().regions[0]], silhouette: [sq(-0.5, -0.5, 0, 0.5)] })
  const d = lg.layerDepth(L, 1)
  const parts = lg.layerBodyParts(L, g, d)
  const bodies = parts.map((part) => ({ part, body: part.build() }))
  const lift = lg.layerLift(bodies, d)
  near(lift, bodies[0].body.half - 0.05, 1e-12)
  assert.ok(lift > 0.1)
  // lowest point: mid-plane (t/2 + lift) − half = 0 above the layer's z
  near(parts[0].z + lift - bodies[0].body.half, 0, 1e-12)
  near(lg.layerBodyHeight(L, g, 1), 2 * bodies[0].body.half, 0.01)
  assert.equal(lg.layerBodyHeight(layerOf(), g, 1), 0.1)
})

test('plate body: front face at z = 0 when placed at −thickness / 2, bevel ≤ thickness / 2', () => {
  const body = lg.buildPlateBody({ shape: 'squircle', cornerRadius: 0.225, thickness: 0.16, bevel: 0.5 })
  near(body.half, 0.08, 1e-9)
  const pos = body.geometry.attributes.position.array
  let maxR = 0
  for (let i = 0; i < pos.length; i += 3) maxR = Math.max(maxR, Math.abs(pos[i]), Math.abs(pos[i + 1]))
  near(maxR, 1, 1e-6)
  assert.equal(lg.buildPlateBody({ shape: 'none', cornerRadius: 0, thickness: 0.16, bevel: 0.04 }).geometry.attributes.position, undefined)
})

test('touching rings (a hole on the outer edge, two holes sharing a vertex): no flattened ear-clip fallback', () => {
  const sq = (x0, y0, x1, y1, hole = false) => ({ ...poly([[x0, y0], [x1, y0], [x1, y1], [x0, y1]]), hole })
  const cases = [
    // pathops output can do both; mathutils' CDT merges the shared constraints, poly2tri needs them separated
    [[sq(-0.5, -0.5, 0.5, 0.5), sq(0.1, -0.2, 0.5, 0.2, true)], 0.84],
    [[sq(-0.5, -0.5, 0.5, 0.5), sq(-0.3, -0.3, 0, 0, true), sq(0, 0, 0.3, 0.3, true)], 0.82],
  ]
  for (const [splines, area] of cases) {
    const { arr, mesh } = build(splines, 0.1, 0.045)
    assert.equal(arr.info.fallbacks, 0, 'poly2tri (with Steiner points), not the ear-clipping fallback')
    near(arr.info.half, 0.05, 1e-9, 'full thickness (the fallback had no interior points: a 0.01 slab)')
    assertClean(mesh, `area ${area}`)
    let a = 0
    for (let t = 0; t < mesh.tris.length / 6; t++) {
      const [i, j, k] = [mesh.tris[3 * t], mesh.tris[3 * t + 1], mesh.tris[3 * t + 2]].map((v) => 3 * v)
      const V = mesh.verts
      a += ((V[j] - V[i]) * (V[k + 1] - V[i + 1]) - (V[k] - V[i]) * (V[j + 1] - V[i + 1])) / 2
    }
    near(a, area, 1e-6, 'top covers exactly the outer minus its holes')
  }
})

// ------------------------------------------------------------------------------------------------ Poisson dome (round 7)
/** Deterministic uniform numbers in [0, 1). */
function rng(seed) {
  let s = seed >>> 0
  return () => (s = (Math.imul(s, 1664525) + 1013904223) >>> 0) / 4294967296
}
/** An irregular Delaunay mesh of a disc: `n` rim points ON the circle + `m` random interior points (poly2tri + flips). */
function discMesh(R = 0.3, n = 72, m = 220, seed = 3, cx = 0) {
  const rim = Array.from({ length: n }, (_, i) => [cx + R * Math.cos((2 * Math.PI * i) / n), R * Math.sin((2 * Math.PI * i) / n)])
  // chord tolerance 1, no max edge: the outline is exactly the n rim points (all ON the circle: u = 0 there exactly)
  const o = hf.outline([poly(rim)], 1, 0, 1e-12)
  const ol = new hf.OutlineQuery(o, 0)
  const r = rng(seed)
  const pts = []
  for (let i = 0; i < m; i++) {
    const rr = R * Math.sqrt(0.85 * r())
    const t = 2 * Math.PI * r()
    pts.push(cx + rr * Math.cos(t), rr * Math.sin(t))
  }
  const tri = hf.triangulate(ol, pts)
  const fixed = Uint8Array.from({ length: tri.V.length / 2 }, (_, i) => (i < tri.nOutline ? 1 : 0))
  return { V: tri.V, T: tri.T, fixed, n: tri.nOutline }
}
/** Largest angle (degrees) between adjacent top faces whose corners all lie above 45 % of the half height (worker test). */
function crease(mesh) {
  const V = mesh.verts
  const T = mesh.tris.subarray(0, mesh.tris.length / 2)
  let zmax = 0
  for (let i = 2; i < V.length; i += 3) zmax = Math.max(zmax, V[i])
  const fn = []
  const zt = []
  for (let t = 0; t < T.length; t += 3) {
    const [a, b, c] = [T[t], T[t + 1], T[t + 2]].map((i) => [V[3 * i], V[3 * i + 1], V[3 * i + 2]])
    const u = [b[0] - a[0], b[1] - a[1], b[2] - a[2]]
    const v = [c[0] - a[0], c[1] - a[1], c[2] - a[2]]
    const n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
    const l = Math.hypot(...n)
    fn.push(n.map((x) => x / l))
    zt.push(Math.min(a[2], b[2], c[2]))
  }
  const seen = new Map()
  let worst = 0
  for (let t = 0; t < T.length / 3; t++)
    for (let k = 0; k < 3; k++) {
      const a = T[3 * t + k]
      const b = T[3 * t + ((k + 1) % 3)]
      const key = a < b ? a * 1e7 + b : b * 1e7 + a
      const o = seen.get(key)
      if (o === undefined) {
        seen.set(key, t)
        continue
      }
      if (zt[o] > 0.45 * zmax && zt[t] > 0.45 * zmax) {
        const d = fn[o][0] * fn[t][0] + fn[o][1] * fn[t][1] + fn[o][2] * fn[t][2]
        worst = Math.max(worst, (Math.acos(Math.max(-1, Math.min(1, d))) * 180) / Math.PI)
      }
    }
  return worst
}

test('Poisson constants and the in-layer piece rules mirror blender_worker (heightfield.py, scene.py)', () => {
  const src = read('blender_worker/heightfield.py')
  for (const k of ['BISECT', 'POISSON_F', 'POISSON_TOL', 'POISSON_MAX_ITER']) assert.equal(hf[k], pyConst(src, k), k)
  const scene = read('blender_worker/scene.py')
  for (const k of ['STACK_GAP', 'INSET_GAP', 'TOUCH_TOL']) assert.equal(lg[k], pyConst(scene, k), k)
})

test('poisson: −∇²u = 4 reproduces a disc exactly (R² − r²) on an irregular Delaunay mesh; quadratic vertex gradient', () => {
  const R = 0.3
  const { V, T, fixed, n } = discMesh(R)
  const exact = (i) => (i < n ? 0 : R * R - V[2 * i] ** 2 - V[2 * i + 1] ** 2)
  const tight = hf.poisson(V, T, fixed, 4, 1e-10)
  let err = 0
  for (let i = 0; i < tight.u.length; i++) err = Math.max(err, Math.abs(tight.u[i] - exact(i)))
  assert.ok(err < 1e-7 * R * R, `exact at every vertex (${err})`)
  const { u, info } = hf.poisson(V, T, fixed) // the default stop: ample for a height field
  err = 0
  for (let i = 0; i < u.length; i++) err = Math.max(err, Math.abs(u[i] - exact(i)))
  assert.ok(err < 1e-3 * R * R, `${err}`)
  assert.ok(info.residual <= hf.POISSON_TOL && info.iterations > 0 && info.iterations < 300, JSON.stringify(info))
  // the vertex gradient: exact for linear fields, −2x for the disc (least-squares quadratic fit)
  const lin = Float64Array.from({ length: V.length / 2 }, (_, i) => 2 * V[2 * i] - 0.5 * V[2 * i + 1])
  const gl = hf.vertexGradient(V, T, lin)
  for (let i = 0; i < gl.length / 2; i++) {
    near(gl[2 * i], 2, 1e-9, 'gx')
    near(gl[2 * i + 1], -0.5, 1e-9, 'gy')
  }
  const gu = hf.vertexGradient(V, T, tight.u)
  for (let i = n; i < V.length / 2; i++)
    if (Math.hypot(V[2 * i], V[2 * i + 1]) < 0.2) {
      near(gu[2 * i], -2 * V[2 * i], 1e-6, 'disc gx')
      near(gu[2 * i + 1], -2 * V[2 * i + 1], 1e-6, 'disc gy')
    }
})

test('poisson: separate parts are components of the free vertices (solved and normalised apart)', () => {
  const a = discMesh(0.2, 48, 80, 1)
  const b = discMesh(0.1, 48, 40, 2, 0.6)
  const off = a.V.length / 2
  const V = a.V.concat(b.V)
  const T = a.T.concat(b.T.map((i) => i + off))
  const fixed = Uint8Array.from([...a.fixed, ...b.fixed])
  const { comp, count } = hf.components(V.length / 2, T, fixed)
  assert.equal(count, 2)
  for (let i = 0; i < fixed.length; i++) assert.equal(comp[i] === -1, !!fixed[i])
  assert.equal(new Set(Array.from(comp.subarray(0, off)).filter((c) => c >= 0)).size, 1)
})

test('inflated bodies = the worker: a Poisson dome (round tube on thin parts, no fins) with half height t/2 + k·D', () => {
  // an ellipse x²/a² + y²/b² ≤ 1: u = c(1 − x²/a² − y²/b²) → dome = k·D·√(1 − x²/a² − y²/b²)
  const a = 0.5
  const bb = 0.12
  const el = Array.from({ length: 96 }, (_, i) => [a * Math.cos((2 * Math.PI * i) / 96), bb * Math.sin((2 * Math.PI * i) / 96)])
  const { arr, mesh } = build([poly(el)], 0.1, 0.03, 1, 6)
  assertClean(mesh, 'ellipse')
  near(arr.info.half, 0.05 + arr.info.D, 1e-6 * arr.info.half, 'q = 1 at the apex: H = t + 2·k·D')
  assert.ok(arr.info.poisson && arr.info.poisson.residual <= hf.POISSON_TOL && arr.info.poisson.components === 1)
  // a thin stadium (bevel 0, wall e = t/2): a ROUND tube (semicircular cross-section) — not a creased ridge
  const w = 0.04
  const st = []
  for (let i = 0; i < 9; i++) st.push([-0.4 + (0.8 * i) / 8, -w])
  for (let i = 0; i < 6; i++) {
    const t = 0.3 + ((Math.PI - 0.6) * i) / 5
    st.push([0.4 + w * Math.sin(t), -w * Math.cos(t)])
  }
  for (let i = 0; i < 9; i++) st.push([0.4 - (0.8 * i) / 8, w])
  for (let i = 0; i < 6; i++) {
    const t = 0.3 + ((Math.PI - 0.6) * i) / 5
    st.push([-0.4 - w * Math.sin(t), w * Math.cos(t)])
  }
  const tube = build([poly(st)], 0.02, 0, 1, 6).mesh
  const mid = topVerts(tube).filter((v) => Math.abs(v[0]) < 0.15)
  const ridge = Math.max(...mid.map((v) => v[2]))
  for (const v of mid)
    if (Math.abs(v[1]) < 0.9 * w) {
      const rel = (v[2] - 0.01) / (ridge - 0.01)
      assert.ok(Math.abs(rel - Math.sqrt(1 - (v[1] / w) ** 2)) < 0.08, `tube cross-section at y=${v[1]}`)
    }
  // the corpus shapes whose distance dome grew fins (Gemini's tips): adjacent upper-dome faces meet at < 30°, and the
  // body equals the worker's (half height / volume measured with blender_worker.heightfield.build, thickness 0.16,
  // bevel 0.08, 8 segments — the import defaults)
  // (round 8: the 15 % minimum wall and the locally capped round edge; inflate 0 = the flat import body)
  const REF = {
    'gemini-star.json|0|0.5': { half: 0.245228, volume: 0.1966417 },
    'gemini-star.json|0|1': { half: 0.415896, volume: 0.3141193 },
    'contacts.json|0|1': { half: 0.320313, volume: 0.1267213 },
    'contacts.json|1|1': { half: 0.270939, volume: 0.0477233 },
    'gemini-star.json|0|0': { half: 0.07456, volume: 0.0791435 },
    'contacts.json|0|0': { half: 0.07456, volume: 0.0376607 },
    'contacts.json|1|0': { half: 0.07456, volume: 0.0160825 },
  }
  for (const [key, ref] of Object.entries(REF)) {
    const [file, ri, k] = key.split('|')
    const fx = fixture(file)
    const S = fx.artScale
    const r = build(fx.regions[Number(ri)].splines, 0.16 / S, 0.08 / S, Number(k), 8, S)
    const c = assertClean(r.mesh, key)
    near(r.arr.info.half, ref.half, 0.002 * ref.half, `${key} half`)
    near(c.volume, ref.volume, 0.005 * ref.volume, `${key} volume`)
    if (file === 'gemini-star.json' && k !== '0') assert.ok(crease(r.mesh) < 30, `${key}: crease ${crease(r.mesh).toFixed(1)}°`)
  }
})

test('dome rows sample thin parts ACROSS (per-ray rows scaled to the local width), all inside the piece', () => {
  const strip = poly([[-0.4, -0.03], [0.4, -0.03], [0.4, 0.03], [-0.4, 0.03]])
  const o = hf.outline([strip], hf.CHORD_TOL, hf.MAX_EDGE, hf.MERGE_EPS, 0.005)
  const ol = new hf.OutlineQuery(o, hf.NEAR)
  const D = hf.islandInradius(ol, 40)
  const pts = hf.steinerPoints(ol, D, 0.01, 1, 6, hf.MAX_EDGE, hf.TAN_MIN, 1)
  for (let i = 0; i < pts.length; i += 2) assert.ok(Math.abs(pts[i]) < 0.4 && Math.abs(pts[i + 1]) < 0.03, 'inside')
  const rowsOf = (P) => {
    const ys = new Set()
    for (let i = 0; i < P.length; i += 2) if (Math.abs(P[i]) < 0.2) ys.add(Math.round(Math.abs(P[i + 1]) * 1e4) / 1e4)
    return [...ys]
  }
  const rows = rowsOf(pts)
  assert.ok(rows.length >= 5, `spine + ≥ 4 rows: ${rows}`)
  assert.ok(Math.min(...rows) < 1e-3, 'the spine (medial points)')
  const flat = hf.steinerPoints(ol, D, 0.01, 0, 6, hf.MAX_EDGE, hf.TAN_MIN, 1) // no inflate: unchanged sampling
  assert.ok(rowsOf(flat).length < rows.length)
})

// ------------------------------------------------------------------------------------------------ pieces of one layer
test('pieces of one layer: touching ones pull back INSET_GAP, overlapping / coincident ones stack by real height', () => {
  const tol = 3 * hf.CHORD_TOL
  const rings = (x0, y0, x1, y1) => hf.pieceRings([sq(x0, y0, x1, y1)])
  const a = rings(-0.4, -0.2, 0, 0.2)
  const b = rings(0, -0.3, 0.5, 0.3) // shares the edge x = 0 with a
  const c = rings(-0.2, -0.1, 0.3, 0.1) // overlaps both
  const d = rings(0.7, -0.1, 0.9, 0.1) // apart
  assert.equal(hf.ringsRelation(a, b, tol), 1)
  assert.equal(hf.ringsRelation(a, c, tol), 2)
  assert.equal(hf.ringsRelation(a, d, tol), 0)
  const ai = hf.insetRings(a, b, 0.003)
  const xs = ai.flatMap((r) => Array.from(r).filter((_, i) => i % 2 === 0))
  near(Math.max(...xs), -0.003, 1e-12, 'pulled back from the shared edge')
  near(Math.min(...xs), -0.4, 1e-12, 'the rest unchanged')
  assert.equal(hf.ringsRelation(ai, b, tol), 0)
  const spl = hf.ringsToSplines(ai)
  assert.ok(spl[0].closed && spl[0].points.length === ai[0].length / 2)
  // coincident outlines (a translucent overlay on its base's outline) OVERLAP; a disc filling a ring's hole TOUCHES
  const s0 = rings(-0.4, -0.4, 0.4, 0.4)
  for (const o of [rings(-0.4, -0.4, 0.4, 0.4), rings(-0.399, -0.399, 0.399, 0.399), rings(-0.4005, -0.4, 0.4, 0.4005)]) {
    assert.equal(hf.ringsRelation(s0, o, tol), 2)
    assert.equal(hf.ringsRelation(o, s0, tol), 2)
  }
  const left = rings(-0.4, -0.2, 0.0005, 0.2)
  const right = rings(0, -0.3, 0.5, 0.3)
  assert.equal(hf.ringsRelation(left, right, tol), 1)
  assert.equal(hf.ringsRelation(right, left, tol), 1)
  const ring = hf.pieceRings([circle(0.3), circle(0.15, 0, 0, true)])
  assert.equal(hf.ringsRelation(ring, hf.pieceRings([circle(0.15)]), tol), 1)
  // real-height stacking: c (half 0.05) on a (half 0.05); b only touches → no shift
  const s = hf.stackShifts(3, [[0, 2]], [0.05, 0.05, 0.05], 0.002)
  assert.deepEqual(Array.from(s).map((v) => Math.round(v * 1e9) / 1e9), [0, 0, 0.102])
})

test('layer layout mirrors scene._layer / _body_height: inset touching pieces, stacked overlapping pieces', () => {
  // two touching pieces with their own materials (no automatic merge): the lower one is built from inset splines
  const L = layerOf({ elementMaterials: { a: { preset: 'chrome', params: {} } } })
  const g = geometryOf({
    hash: 'touch',
    silhouette: [sq(-0.4, -0.3, 0.5, 0.3)],
    regions: [
      { elementId: 'a', paint: solid, opacity: 1, zSub: 0, splines: [sq(-0.4, -0.2, 0, 0.2)] },
      { elementId: 'b', paint: solid, opacity: 1, zSub: 0, splines: [sq(0, -0.3, 0.5, 0.3)] },
    ],
  })
  const parts = lg.layerBodyParts(L, g, lg.layerDepth(L, 1))
  assert.match(parts[0].key, /\|inset\|/)
  assert.doesNotMatch(parts[1].key, /\|inset\|/)
  const b0 = parts[0].build().geometry.getAttribute('position')
  let xmax = -Infinity
  for (let i = 0; i < b0.count; i++) xmax = Math.max(xmax, b0.getX(i))
  near(xmax, -lg.INSET_GAP, 1e-6, 'the inset body stops INSET_GAP short of the shared edge')
  // a translucent disc over a square in ONE layer: stacked on it by their real heights (no interpenetration)
  const over = geometryOf({
    hash: 'overlap',
    silhouette: [sq(-0.5, -0.5, 0.5, 0.5)],
    regions: [
      { elementId: 'a', paint: solid, opacity: 1, zSub: 0, splines: [sq(-0.5, -0.5, 0.5, 0.5)] },
      { elementId: 'b', paint: solid, opacity: 0.5, zSub: 0, splines: [circle(0.2)] },
    ],
  })
  const L2 = layerOf({ depth: { z: 0, thickness: 0.1, bevel: 0.03, bevelSegments: 6, inflate: 0.5 } })
  const d = lg.layerDepth(L2, 1)
  const ps = lg.layerBodyParts(L2, over, d)
  const pairs = lg.partStackPairs(ps, over, 1)
  assert.deepEqual(pairs, [[0, 1]])
  const bodies = ps.map((p) => p.build())
  const lift = lg.layerLift(ps.map((part, i) => ({ part, body: bodies[i] })), d)
  const base = ps.map((p) => p.z + lift)
  const sh = lg.partShifts(pairs, bodies, base, 1)
  assert.equal(sh[0], 0)
  // the disc's lowest point clears the square's top by STACK_GAP
  near(base[1] + sh[1] - bodies[1].half, base[0] + bodies[0].half + lg.STACK_GAP, 1e-9)
  // the layer's height (framing / Re-stack) is that in-layer stack, measured like the worker (disc model halves)
  const H = lg.layerBodyHeight(L2, over, 1)
  const top = base[1] + sh[1] + bodies[1].half
  assert.ok(Math.abs(H - top) < 0.03, `H ${H} vs real top ${top}`)
  assert.ok(H > 0.1 + 2 * 0.5 * 0.2, 'taller than one body')
  // with maxRadius in the bundle and no overlaps: H = thickness + 2·inflate·maxRadius·S exactly (presets "geometry")
  const one = geometryOf({ hash: 'mr', regions: [geometryOf().regions[0]], maxRadius: 0.25 })
  near(lg.layerBodyHeight(L2, one, 1.5), 0.1 + 2 * 0.5 * 0.25 * 1.5, 1e-12)
})

// Worker references (fixtures/round8-parity.json, from blender_worker/heightfield.py on corpus pieces).
const r8 = fixture('round8-parity.json')
/** The worker's outline of a fixture piece at thickness t / bevel bv (world units) and its scale S. */
function workerOutline(key, t, bv) {
  const S = r8.scale[key]
  const b = hf.rimBevel(t / S, bv / S)
  const tol = hf.CHORD_TOL / S
  const o = hf.outline(r8.pieces[key], tol, hf.MAX_EDGE / S, hf.MERGE_EPS / S, Math.min(Math.max(0.5 * b, hf.GUARD_MIN / S), hf.GUARD_MAX / S))
  const ol = new hf.OutlineQuery(o, Math.max(hf.NEAR / S, 16 * tol))
  return { o, ol, b, tol, D: hf.islandInradius(ol, 40) }
}

test('local half-width = the worker at sharp corners: a probe nearest to a shared vertex takes the worker’s (float32) segment', () => {
  // Scandit's dot (thin) and Earth's coast (import defaults): tangent discs that reach a vertex shared by two segments
  // decided the material side by whichever segment the float64 search hit first — Scandit's vertex 53 came out 9.6e-4
  // narrower (just above its bevel), Earth's vertex 169 capped 17 % of the bevel lower than the worker (a dent in the
  // round edge that Cycles does not have).
  for (const key of ['scanditR0', 'earthSil']) {
    const ref = r8.localWidth[key]
    const { o, ol, b, tol, D } = workerOutline(key, ref.thickness, ref.bevel)
    assert.deepEqual(o.rings.map((r) => r.length >> 1), ref.rings, `${key}: the worker's sample rings`)
    const wv = hf.localWidth(ol, D, hf.WIDTH_TOL * tol, b)
    // the round edge only sees min(w, b) (rimRadius); widths above the bevel may differ (wide parts, not capped)
    let worst = 0
    for (let i = 0; i < wv.length; i++) worst = Math.max(worst, Math.abs(Math.min(wv[i], b) - Math.min(ref.wv[i], b)))
    // (one late bisection step of 1.05·D / 2^10 may still differ: ≤ 0.3 % of the bevel here, 16.9 % without the tie-break)
    assert.ok(worst < 0.01 * b, `${key}: capped local half-width within ${worst.toExponential(2)} of the worker (bevel ${b.toFixed(4)})`)
  }
})

test('rings touching another ring mid-edge (a T-junction): a coarser jitter retry instead of a flat ear-clipped body', () => {
  // Home's silhouette and Scandit's dot at layer scale 0.7, thickness 0.3, bevel 0.15, inflate 0.3: poly2tri's
  // "Collinear not supported" at the 1e-8 input jitter used to fall back to ear clipping (no Steiner points: a FLAT
  // body of the wall height) while the worker's CDT domes them.
  for (const key of ['homeSil', 'scanditR4']) {
    const ref = r8.fallback[key]
    const S = r8.scale[key] * ref.scaleMul
    const { arr, mesh } = build(r8.pieces[key], ref.thickness / S, ref.bevel / S, ref.inflate, ref.segments, S)
    assert.equal(arr.info.fallbacks, 0, `${key}: no ear-clipping fallback`)
    near(arr.info.half, ref.half, 1e-4 * ref.half + 1e-6, `${key}: half height = the worker's`)
    assertClean(mesh, key)
  }
})

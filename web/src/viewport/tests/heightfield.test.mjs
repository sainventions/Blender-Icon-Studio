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
    'CHORD_PASSES',
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
  for (const r of fx.regions) {
    const { arr, mesh } = build(r.splines, t, b, 0, 8, S)
    assertClean(mesh, r.elementId)
    // e = 0: no vertical wall; the island is thinner than the edge radius, so it tapers to √(b² − (b − D)²)
    assert.equal(arr.info.wall, 0)
    if (arr.info.D < b) near(arr.info.half, Math.sqrt(b * b - (b - arr.info.D) ** 2), 0.01 * t, `${r.elementId} bead`)
  }
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

// CAD-style iso view of the live view (PLAN §11 View): an orthographic camera slerped head-on → isometric showing the
// REAL layer distances (viewport/scene/iso.ts = blender_worker/framing.py iso_basis / ortho_plan) and the layer stack
// it frames (viewport/scene/stack.ts). Run (from web/): node --test src/viewport/tests/
import { presets, read } from './helpers.mjs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const iso = await import('../scene/iso.ts')
const stack = await import('../scene/stack.ts')
const shapes = await import('../../lib/shapes.ts')
const rig = await import('../scene/rig.ts')

const near = (a, b, eps, msg = '') => assert.ok(Math.abs(a - b) <= eps, `${msg} ${a} ≉ ${b} (±${eps})`)
const deg = (r) => (r * 180) / Math.PI

test('margins and the iso basis mirror blender_worker/framing.py', () => {
  const src = read('blender_worker/framing.py')
  assert.match(src, /FRONT_MARGIN = \(1\.0 - 2\.0 \/ 2\.24\) \/ 2\.0/)
  near(iso.FRONT_MARGIN, (1 - 2 / 2.24) / 2, 1e-15)
  assert.match(src, /np\.array\(\[1\.0, 1\.0, 0\.0\]\) \/ math\.sqrt\(2\.0\), np\.array\(\[-1\.0, 1\.0, 2\.0\]\) \/ math\.sqrt\(6\.0\)/)
  // head-on: the front camera (looking −Z, art +Y up)
  const b0 = iso.isoBasis(0)
  near(b0.x.x, 1, 1e-12)
  near(b0.y.y, 1, 1e-12)
  near(b0.z.z, 1, 1e-12)
  // isometric: camera x = (1, 1, 0)/√2, y = (−1, 1, 2)/√6, view along −(1, −1, 1)/√3 — elevation 35.264°, azimuth 45°
  const b1 = iso.isoBasis(1)
  const s2 = Math.SQRT1_2
  const s6 = 1 / Math.sqrt(6)
  const s3 = 1 / Math.sqrt(3)
  ;[[b1.x, [s2, s2, 0]], [b1.y, [-s6, s6, 2 * s6]], [b1.z, [s3, -s3, s3]]].forEach(([v, e], i) => {
    near(v.x, e[0], 1e-9, `axis ${i} x`)
    near(v.y, e[1], 1e-9, `axis ${i} y`)
    near(v.z, e[2], 1e-9, `axis ${i} z`)
  })
  near(deg(Math.asin(b1.z.z)), 35.264, 1e-3)
  // the slerp in between (the server review measured 59.4° camera elevation at iso 0.55 in Blender)
  near(deg(Math.asin(iso.isoBasis(0.55).z.z)), 59.4, 0.1)
  // monotonic, smooth
  let prev = 90
  for (let t = 0.05; t <= 1.0001; t += 0.05) {
    const e = deg(Math.asin(iso.isoBasis(t).z.z))
    assert.ok(e < prev && prev - e < 4, `elevation at ${t.toFixed(2)}: ${e}`)
    prev = e
  }
  near(iso.clampIso(3), 1, 0)
  near(iso.clampIso(-1), 0, 0)
  near(iso.clampIso(undefined), 0, 0)
})

test('ortho framing (framing.ortho_plan): iso 0 continues the front view, iso views fit the subject with the same border', () => {
  const pts = []
  for (const [x, y] of shapes.plateOutline('squircle', 0.225, 64)) pts.push(x, y, 0, x, y, -0.16)
  const n = pts.length / 3
  const f0 = iso.orthoFrame(pts, n, 0, 1)
  near(f0.scale, 2.24, 1e-3)
  near(f0.cx, 0, 1e-9)
  near(f0.cy, 0, 1e-9)
  near(iso.orthoFrame(pts, n, 0, 2).scale, 1.12, 1e-3)
  for (const t of [0.3, 0.55, 1]) {
    const f = iso.orthoFrame(pts, n, t, 1)
    const { x, y } = iso.isoBasis(t)
    let worst = 0
    for (let i = 0; i < n; i++) {
      const dx = pts[3 * i] - f.target.x
      const dy = pts[3 * i + 1] - f.target.y
      const dz = pts[3 * i + 2] - f.target.z
      const u = dx * x.x + dy * x.y + dz * x.z - f.cx
      const v = dx * y.x + dy * y.y + dz * y.z - f.cy
      worst = Math.max(worst, Math.abs(u), Math.abs(v))
    }
    // the larger extent leaves exactly the front view's border on each side
    near(worst, (f.scale / 2) * (1 - 2 * iso.FRONT_MARGIN), 1e-9, `t=${t}`)
  }
  assert.equal(iso.orthoFrame([], 0, 0.5, 1).scale, 2.24)
})

test('hull2d: convex hull, CCW', () => {
  const h = iso.hull2d([0, 0, 1, 0, 1, 1, 0, 1, 0.5, 0.5, 0.2, 0.9])
  assert.deepEqual(h, [0, 0, 1, 0, 1, 1, 0, 1])
})

const poly = (pts) => ({ closed: true, hole: false, parent: -1, depth: 0, points: pts.map((p) => ({ co: p, hl: p, hr: p })) })
const sq = (x0, y0, x1, y1) => poly([[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
function layer(id, z, preset, over = {}) {
  return {
    id,
    name: id,
    elementIds: [`${id}e`],
    visible: true,
    locked: false,
    mode: 'individual',
    fill: { type: 'auto' },
    opacity: 1,
    blendMode: 'normal',
    glass: true,
    transform: { x: 0, y: 0, scale: 1 },
    depth: { z, thickness: 0.1, bevel: 0.045, bevelSegments: 6, inflate: 0 },
    material: { preset, params: {} },
    shadow: { kind: 'physical', opacity: 0.5 },
    ...over,
  }
}
function geo(id, x0, y0, x1, y1) {
  return {
    layerId: id,
    hash: `h-${id}`,
    silhouette: [sq(x0, y0, x1, y1)],
    regions: [{ elementId: `${id}e`, paint: { type: 'solid', color: '#3366ff', opacity: 1 }, opacity: 1, zSub: 0, splines: [sq(x0, y0, x1, y1)] }],
    safeRadius: 0.2,
    bbox: [x0, y0, x1, y1],
    texture: '',
    texturePath: '',
    svg: '',
    images: [],
  }
}

test('layers sit at their REAL z (depth.z + ε) — no explode spreading; the framing hull spans their real heights', () => {
  const layers = [layer('A', 0, 'satin'), layer('B', 0.25, 'liquid_glass'), layer('C', 0.5, 'liquid_glass', { visible: false })]
  const geometry = { A: geo('A', -0.6, -0.6, 0.2, 0.2), B: geo('B', -0.2, -0.2, 0.6, 0.6), C: geo('C', 0, 0, 1, 1) }
  const art = { scale: 1, x: 0, y: 0 }
  const s = stack.buildStack(layers, geometry, art, presets)
  assert.deepEqual(s.map((e) => e.layer.id), ['A', 'B'])
  near(s[0].z, 0.002, 1e-12)
  near(s[1].z, 0.252, 1e-12)
  near(stack.stackTop(s), 0.352, 1e-12)
  // the worker ignores camera.explode (legacy): nothing in the stack depends on it
  const code = read('web/src/viewport/scene/stack.ts')
    .split(/\r?\n/)
    .filter((l) => !l.trim().startsWith('//'))
    .join(' ')
  assert.ok(!/explode/i.test(code))
  const out = []
  const n = stack.stackFramePoints(s, { outline: shapes.plateOutline('squircle', 0.225, 16), thickness: 0.16 }, out)
  const zs = out.filter((_, i) => i % 3 === 2)
  near(Math.min(...zs), -0.16, 1e-12)
  near(Math.max(...zs), 0.352, 1e-12)
  assert.equal(n, out.length / 3)
  // without a plate the canvas square at z = 0 stands in (iso 0 continues the front framing)
  const out2 = []
  stack.stackFramePoints(s, null, out2)
  assert.deepEqual(out2.slice(0, 12), [-1, -1, 0, 1, -1, 0, 1, 1, 0, -1, 1, 0])
})

test('contact: glass lying on the plate / a lower layer is seen against light that crossed it', () => {
  const art = { scale: 1, x: 0, y: 0 }
  const geometry = { A: geo('A', -0.6, -0.6, 0.6, 0.6), B: geo('B', -0.2, -0.2, 0.2, 0.2), C: geo('C', 0.62, 0.62, 0.9, 0.9) }
  // A on the plate, B on A (gap 0.03), C floating 0.4 above the plate beside A (its size 0.14)
  const s = stack.buildStack([layer('A', 0, 'liquid_glass'), layer('B', 0.13, 'liquid_glass'), layer('C', 0.4, 'liquid_glass')], geometry, art, presets)
  near(s[0].contact, 1 - 0.002 / 0.6, 1e-9)
  near(s[1].contact, 1 - (0.132 - 0.102) / 0.2, 1e-9)
  assert.equal(s[2].contact, 0)
  // without a plate nothing lies under A
  assert.equal(stack.buildStack([layer('A', 0, 'liquid_glass')], geometry, art, presets, false)[0].contact, 0)
  // hull overlap (not bboxes): a convex quad clipped by another
  near(iso.convexOverlapArea([0, 0, 1, 0, 1, 1, 0, 1], [0.5, 0.5, 1.5, 0.5, 1.5, 1.5, 0.5, 1.5]), 0.25, 1e-12)
  assert.equal(iso.convexOverlapArea([0, 0, 1, 0, 1, 1, 0, 1], [2, 2, 3, 2, 3, 3, 2, 3]), 0)
})

test('glass seen through glass + translucent pass routing flags', () => {
  const art = { scale: 1, x: 0, y: 0 }
  const geometry = { A: geo('A', -0.6, -0.6, 0.2, 0.2), B: geo('B', -0.2, -0.2, 0.6, 0.6), C: geo('C', 0.7, 0.7, 0.9, 0.9) }
  let s = stack.buildStack([layer('A', 0, 'liquid_glass'), layer('B', 0.13, 'clear_glass'), layer('C', 0.26, 'liquid_glass')], geometry, art, presets)
  assert.deepEqual(s.map((e) => [e.transmissive, e.glassAbove]), [[true, true], [true, false], [true, false]])
  // a translucent solid above refracting glass stays in the transparent pass (drawn after the glass, over it)
  s = stack.buildStack([layer('A', 0, 'liquid_glass'), layer('B', 0.13, 'satin', { opacity: 0.5 })], geometry, art, presets)
  assert.deepEqual(s.map((e) => e.routeBlended), [true, false])
  // per-shape materials are resolved for each body (one Principled per shape)
  s = stack.buildStack([layer('A', 0, 'liquid_glass', { elementMaterials: { Ae: { preset: 'chrome', params: {} } } })], geometry, art, presets)
  const params = [...s[0].params.values()]
  assert.equal(params.length, 1)
  assert.equal(params[0].metallic, 1)
  assert.equal(s[0].transmissive, false)
})

test('framing hull = worker subject_hulls: an extruded raster region is bounded by its contour, a flat card by its placement quad', () => {
  const art = { scale: 1, x: 0, y: 0 }
  // a 0.4-wide traced contour whose 1000 px PNG placement overhangs it to ±0.9 (Find Device), plus a flat card rotated
  // 45° (matrix placement: its quad, not its axis-aligned bbox)
  const g = geo('A', -0.2, -0.2, 0.2, 0.2)
  const r = Math.SQRT1_2 * 0.001
  g.images = [
    { elementId: 'Ae', path: 'a.png', url: '/a.png', bbox: [-0.9, -0.9, 0.9, 0.9], opacity: 1, matrix: [0.0018, 0, 0, -0.0018, -0.9, 0.9], width: 1000, height: 1000 },
    { elementId: 'card', path: 'c.png', url: '/c.png', bbox: [0.3, -0.3, 0.7, 0.3], opacity: 1, matrix: [r, r, r, -r, 0.3, 0], width: 200, height: 200 },
  ]
  const [e] = stack.buildStack([layer('A', 0, 'liquid_glass')], { A: g }, art, presets)
  const xs = e.hull.filter((_, i) => i % 2 === 0)
  const ys = e.hull.filter((_, i) => i % 2 === 1)
  near(Math.min(...xs), -0.2, 1e-9, 'contour, not the overhanging PNG')
  near(Math.min(...ys), -0.2, 1e-9)
  near(Math.max(...xs), 0.3 + 2 * 0.1414, 1e-3, 'rotated card quad reaches its far corner')
  near(Math.max(...ys), 0.2, 1e-9, 'card quad corners (±0.1414), not its loose bbox (±0.3)')
  // without the card the hull is the contour alone
  g.images = g.images.slice(0, 1)
  const [e2] = stack.buildStack([layer('A', 0, 'liquid_glass')], { A: { ...g, hash: 'h-A2' } }, art, presets)
  near(Math.max(...e2.hull.filter((_, i) => i % 2 === 0)), 0.2, 1e-9)
})

test('live key shadow blur follows the tallest caster (soft Cycles penumbra under thick glass / inflated domes)', () => {
  assert.equal(rig.shadowGap(0.1), rig.SHADOW_GAP, 'default 0.1-thick layers keep the round-5 contact gap')
  assert.equal(rig.shadowGap(0), rig.SHADOW_GAP)
  assert.equal(rig.shadowGap(Number.NaN), rig.SHADOW_GAP)
  near(rig.shadowGap(0.4), 0.2, 1e-12, 'a 0.4-high sphere head casts from ~half its height')
  assert.equal(rig.shadowGap(5), rig.SHADOW_GAP_MAX)
  // the stack reports the bodies' REAL heights (an inflated dome is taller than its thickness)
  const art = { scale: 1, x: 0, y: 0 }
  const s = stack.buildStack(
    [layer('A', 0, 'liquid_glass'), layer('B', 0.13, 'liquid_glass', { depth: { z: 0.13, thickness: 0.1, bevel: 0.045, bevelSegments: 6, inflate: 1 } })],
    { A: geo('A', -0.6, -0.6, 0.6, 0.6), B: geo('B', -0.3, -0.3, 0.3, 0.3) },
    art,
    presets,
  )
  near(s[0].height, 0.1, 1e-12)
  assert.ok(s[1].height > 0.5, `inflated height ${s[1].height}`)
  assert.ok(rig.shadowGap(Math.max(...s.map((e) => e.height))) > 0.25)
})

test('camera-relative lighting (round 7): rig + environment turn with the camera; head-on is unchanged', async () => {
  const THREE = await import('three')
  const id = new THREE.Quaternion()
  // head-on: exactly the round-6 placement (light angle about the view axis, environment Rz(−angle))
  for (const a of [-45, 0, 30, 135]) {
    const d = rig.viewLightDir(a, 50, id)
    const r = rig.lightDir(a, 50)
    near(d.distanceTo(r), 0, 1e-12, `angle ${a}`)
    const e = rig.environmentRotation(a, id)
    near(e.x, 0, 1e-12)
    near(e.y, 0, 1e-12)
    near(e.z, -THREE.MathUtils.degToRad(a), 1e-9, `env angle ${a}`)
  }
  // any view: the light keeps its place on the SCREEN (camera frame), and the environment's key spot (rendered for
  // angle 0 at lightDir(0, elevation)) lands on the key light — the cube map and the lights stay one rig
  for (const t of [0.35, 0.6, 1]) {
    const q = iso.isoQuaternion(t)
    const { x, y, z } = iso.isoBasis(t)
    for (const a of [-45, 20]) {
      const d = rig.viewLightDir(a, 50, q)
      const head = rig.lightDir(a, 50)
      near(d.dot(x), head.x, 1e-12, 'screen x')
      near(d.dot(y), head.y, 1e-12, 'screen y')
      near(d.dot(z), head.z, 1e-12, 'toward the viewer')
      const env = new THREE.Quaternion().setFromEuler(rig.environmentRotation(a, q))
      near(rig.lightDir(0, 50).applyQuaternion(env).distanceTo(d), 0, 1e-9, `env key spot at iso ${t}`)
    }
  }
  // at iso 1 the old world-fixed key (−45°, elevation 50°) sat ~5° from the mirror direction of the flat tops (washed
  // out to white); camera-relative, the mirror direction of a flat top (reflect the view about +Z) stays far from it
  const q1 = iso.isoQuaternion(1)
  const view = iso.isoBasis(1).z
  const mirror = new THREE.Vector3(-view.x, -view.y, view.z)
  const ang = (u) => deg(Math.acos(Math.max(-1, Math.min(1, u.dot(mirror)))))
  assert.ok(ang(rig.lightDir(-45, 50)) < 10, `world-fixed key ${ang(rig.lightDir(-45, 50)).toFixed(1)}° from the glare`)
  assert.ok(ang(rig.viewLightDir(-45, 50, q1)) > 40, `camera-relative key ${ang(rig.viewLightDir(-45, 50, q1)).toFixed(1)}°`)
  // the worker turns its rig and world with the camera too (lighting.py `view`), and the live view applies it per frame
  const src = read('blender_worker/lighting.py')
  assert.match(src, /ob\.matrix_world = V4 @ Matrix\.Translation\(d \* dist\) @ _look_rotation\(d\)/)
  assert.match(src, /fwd = tuple\(R @ Vector\(\(0\.0, 0\.0, 1\.0\)\)\)/)
  const live = read('web/src/viewport/scene/StudioLighting.tsx')
  assert.match(live, /environmentRotation\(rig\.angle, q, scene\.environmentRotation\)/)
  assert.match(live, /viewLightDir\(rig\.angle, rig\.elevation, q, L\.dir\)\)\.multiplyScalar\(KEY_DISTANCE\)/)
  assert.match(live, /viewLightDir\(rig\.angle \+ 160, 55, q, L\.dir\)\)\.multiplyScalar\(FILL_DISTANCE\)/)
  assert.ok(!/position=\{\[keyDir/.test(live), 'no world-fixed key position left')
})

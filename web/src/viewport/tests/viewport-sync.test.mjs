// Viewport ⇄ Blender worker parity regression tests (round 3 "viewport-sync").
// Run (from web/): node --test src/viewport/tests/viewport-sync.test.mjs   (Node ≥ 23.6: built-in TS type stripping)
//
// The three.js live preview mirrors blender_worker/ term by term; these tests pin the mirrored constants against the
// worker sources (so a worker change without its viewport twin fails here) and cover the viewport-only fixes: dark
// overlay compositing, smoked glass for dark paints, clear renditions, convex-only fillets / closed fills, and the pill
// geometry's inset clamp + cap untangling (Sheets' grid, Translate's fold tip).
import { register } from 'node:module'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

// Extensionless relative imports inside the app sources ('./appearance') resolve to .ts / .tsx.
register(
  'data:text/javascript,' +
    encodeURIComponent(`
export async function resolve(spec, ctx, next) {
  try {
    return await next(spec, ctx)
  } catch (err) {
    if ((spec.startsWith('./') || spec.startsWith('../')) && !/\\.[cm]?[jt]sx?$/.test(spec)) {
      for (const ext of ['.ts', '.tsx']) {
        try { return await next(spec + ext, ctx) } catch {}
      }
    }
    throw err
  }
}`),
  import.meta.url,
)

const REPO = new URL('../../../../', import.meta.url)
const read = (p) => readFileSync(new URL(p, REPO), 'utf8')
/** `NAME = 0.85` (Python module constant) → number. */
function pyConst(src, name) {
  const m = new RegExp(`^${name}\\s*=\\s*([-0-9.e]+)`, 'm').exec(src)
  assert.ok(m, `${name} not found`)
  return Number(m[1])
}

const THREE = await import('three')
const appearance = await import('../../lib/appearance.ts')
const m3d = await import('../../lib/materials3d.ts')
const rig = await import('../scene/rig.ts')
const fillet = await import('../geometry/fillet.ts')
const flatten = await import('../geometry/flatten.ts')
const pill = await import('../geometry/pillGeometry.ts')
const softAlpha = await import('../textures/softAlpha.ts')
const presets = JSON.parse(read('shared/presets.json'))

// ------------------------------------------------------------------------------------------------ mirrored constants
test('lighting calibration mirrors blender_worker/lighting.py', () => {
  const src = read('blender_worker/lighting.py')
  assert.equal(rig.DIFFUSE_CAL, pyConst(src, 'DIFFUSE_CAL'))
  assert.equal(rig.WORLD_CAL, pyConst(src, 'WORLD_CAL'))
})

test('clear rendition constants mirror blender_worker/appearance.py', () => {
  const src = read('blender_worker/appearance.py')
  const C = appearance.CLEAR
  assert.equal(C.tint, pyConst(src, 'CLEAR_TINT'))
  assert.equal(C.edgeDark, pyConst(src, 'CLEAR_EDGE_DARK'))
  assert.equal(C.lightShadow, pyConst(src, 'CLEAR_LIGHT_SHADOW'))
  assert.equal(C.glowLight, pyConst(src, 'CLEAR_GLOW_LIGHT'))
  assert.equal(C.glowDark, pyConst(src, 'CLEAR_GLOW_DARK'))
  assert.equal(C.lightPlateTint, pyConst(src, 'CLEAR_LIGHT_PLATE_TINT'))
  assert.equal(C.lightPlate, /^CLEAR_LIGHT_PLATE\s*=\s*"(#[0-9a-f]{6})"/m.exec(src)[1])
  assert.equal(appearance.MONO_FLOOR, pyConst(src, 'MONO_FLOOR'))
})

test('geometry constants mirror blender_worker/geometry.py and scene.py', () => {
  const geo = read('blender_worker/geometry.py')
  assert.equal(fillet.ACUTE_GN_DEG, pyConst(geo, 'ACUTE_GN_DEG'))
  assert.equal(fillet.STRAIGHT_TURN, pyConst(geo, 'STRAIGHT_TURN'))
  assert.equal(fillet.STRAIGHT_TOL, pyConst(geo, 'STRAIGHT_TOL'))
  assert.equal(fillet.CUSP_DEG, pyConst(geo, 'CUSP_DEG'))
  assert.equal(fillet.FILLET_RATIO, pyConst(geo, 'FILLET_RATIO'))
  const scene = read('blender_worker/scene.py')
  assert.equal(softAlpha.HALO_SOFT_MIN, pyConst(scene, 'HALO_SOFT_MIN'))
})

test('satin mirrors the worker (Specular IOR Level 0.35, sheen 0.03, coat default 0.15)', () => {
  const src = read('blender_worker/materials.py')
  const satin = /def b_satin[\s\S]*?return s, s, None/.exec(src)[0]
  assert.match(satin, /"Specular IOR Level": 0\.35/)
  assert.match(satin, /"Sheen Weight": 0\.03/)
  assert.equal(presets.materials.satin.params.coat.default, 0.15)
  const s = m3d.describeMaterial({ preset: 'satin', params: {} }, presets)
  assert.equal(s.clearcoat, 0.15)
  assert.equal(s.specularIntensity, 0.7) // three.js specularIntensity 1 ≙ Blender 0.5
  assert.equal(s.sheen, 0.03)
  // no presets → the built-in fallback agrees
  assert.equal(m3d.describeMaterial({ preset: 'satin', params: {} }, null).clearcoat, 0.15)
})

test('Liquid Glass white fill share mirrors the worker (1.45 − 0.6·transl, ≤ 0.97)', () => {
  assert.match(read('blender_worker/materials.py'), /clamp\(1\.45 - 0\.6 \* transl, 0\.0, 0\.97\)/)
  const s = m3d.describeMaterial({ preset: 'liquid_glass', params: { translucency: 0.35 } }, presets)
  assert.ok(Math.abs(s.lg.fill[1] - 0.97) < 1e-9)
  const s2 = m3d.describeMaterial({ preset: 'liquid_glass', params: { translucency: 1 } }, presets)
  assert.ok(Math.abs(s2.lg.fill[1] - 0.85) < 1e-9)
})

// ------------------------------------------------------------------------------------------------ appearances
function project() {
  const layer = (id, shadowOpacity) => ({
    id, name: id, elementIds: [], visible: true, locked: false, mode: 'individual', fill: { type: 'none' }, opacity: 1,
    blendMode: 'normal', glass: false, transform: { x: 0, y: 0, scale: 1 },
    depth: { z: 0, thickness: 0.1, bevel: 0.045, bevelSegments: 6, inflate: 0 },
    material: { preset: 'satin', params: {} }, shadow: { kind: 'none', opacity: shadowOpacity },
  })
  return {
    version: 1, id: 'p', name: 'p', createdAt: '', updatedAt: '', strategy: 'smart', elements: [],
    source: { filename: 'x.svg', viewBox: [0, 0, 1, 1], warnings: [], plateDetected: true },
    layers: [layer('L1', 0.3), layer('L2', 0.95)],
    canvas: {
      platform: 'ios', shape: 'squircle', cornerRadius: 0.2, art: { scale: 1, x: 0, y: 0 },
      plate: { visible: true, fill: { type: 'solid', color: '#ff0000', opacity: 1 }, material: { preset: 'satin', params: {} }, thickness: 0.16, bevel: 0.04 },
    },
    lighting: { preset: 'studio', angle: -45, elevation: 50, intensity: 1, rim: 1, fill: 1, environment: 1, shadowSoftness: 0.5 },
    camera: { view: 'front', tiltX: 0, tiltY: 0, fov: 30, zoom: 1, explode: 1 },
    appearance: 'light',
    appearances: { dark: { plateFill: { type: 'system-dark' }, layers: {} }, mono: { layers: {} }, tint: { color: '#3b82f6', strength: 0.8 } },
    render: { quality: 'draft', colorMode: 'neutral', backdrop: 'transparent', backdropColor: '#1c1c22', autoPreview: true },
  }
}

test('clear-light: tinted mono glass, low glow, edge darkening, deep shadow, smoked pale plate', () => {
  const p = appearance.resolveAppearance(project(), 'clear-light')
  for (const L of p.layers) {
    assert.equal(L.material.preset, 'liquid_glass')
    assert.equal(L.material.params.tint, 0.5)
    assert.equal(L.material.params.glow, 0.1)
    assert.equal(L.material.params.__intent, 'mono')
    assert.equal(L.material.params.__edgeDark, 0.8)
    assert.equal(L.fill.type, 'auto') // fill 'none' → auto, like the worker
    assert.equal(L.shadow.kind, 'neutral')
  }
  assert.equal(p.layers[0].shadow.opacity, 0.8) // floor
  assert.equal(p.layers[1].shadow.opacity, 0.95) // kept when deeper
  assert.deepEqual(p.canvas.plate.material, { preset: 'frosted_glass', params: { tint: 0.4, frost: 0.42, grain: 0.04 } })
  assert.equal(p.canvas.plate.fill.color, '#c9ccd6')
  const s = m3d.describeMaterial(p.layers[0].material, presets)
  assert.equal(s.lg.edgeDark, 0.8)
  assert.equal(s.lg.glow, 0.1)
  assert.ok(Math.abs(s.paintMix - m3d.tintCurve(0.5)) < 1e-12) // dark parts stay dark smoked glass
})

test('clear-dark: moderate glow, no edge darkening, white frosted pane, shadows untouched', () => {
  const p = appearance.resolveAppearance(project(), 'clear-dark')
  for (const L of p.layers) {
    assert.equal(L.material.params.glow, 0.3)
    assert.equal(L.material.params.__edgeDark, undefined)
  }
  assert.equal(p.layers[0].shadow.opacity, 0.3)
  assert.deepEqual(p.canvas.plate.material, { preset: 'frosted_glass', params: { tint: 0, frost: 0.42, grain: 0.04 } })
  assert.equal(p.canvas.plate.fill.color, '#ffffff')
  assert.equal(m3d.describeMaterial(p.layers[0].material, presets).lg.edgeDark, 0)
})

// ------------------------------------------------------------------------------------------------ shader patch
function ctx(over = {}) {
  return {
    paint: { map: null, color: new THREE.Color(0, 0, 0), lumRange: [0, 1] }, thickness: 0.1, fake: null,
    rimDir: new THREE.Vector3(0, 0.7, 0.7), opacity: 1, ...over,
  }
}
function compile(material) {
  const lib = THREE.ShaderLib.physical
  const shader = { uniforms: {}, vertexShader: lib.vertexShader, fragmentShader: lib.fragmentShader }
  const warn = console.warn
  const warnings = []
  console.warn = (...a) => warnings.push(a.join(' '))
  try {
    material.onBeforeCompile(shader)
  } finally {
    console.warn = warn
  }
  return { shader, warnings }
}

test('translucent bodies composite like the SVG (sRGB-space opacity)', () => {
  const spec = m3d.describeMaterial({ preset: 'liquid_glass', params: {} }, presets)
  const m = new m3d.IconMaterial()
  m3d.applyIconMaterial(m, spec, ctx({ opacity: 0.33 }))
  assert.ok('BIS_SRGB_ALPHA' in m.defines)
  m3d.applyIconMaterial(m, spec, ctx({ opacity: 1 }))
  assert.ok(!('BIS_SRGB_ALPHA' in m.defines))
  const { shader, warnings } = compile(m)
  assert.deepEqual(warnings, [], 'every shader anchor must exist in this three.js version')
  assert.match(shader.fragmentShader, /diffuseColor\.a = 1\.0 - pow\( clamp\( 1\.0 - diffuseColor\.a, 0\.0, 1\.0 \), 2\.2 - 1\.5 \* bisLightness\( bisP \) \)/)
  // a 33 % black overlay must keep 0.67 of the sRGB value beneath → linear alpha 1 − 0.67^2.2 ≈ 0.586
  const L = 0
  assert.ok(Math.abs(1 - (1 - 0.33) ** (2.2 - 1.5 * L) - 0.586) < 0.002)
})

test('Liquid Glass: smoked glass for dark paints, edge darkening, transmission tint split', () => {
  const m = new m3d.IconMaterial()
  m3d.applyIconMaterial(m, m3d.describeMaterial({ preset: 'liquid_glass', params: {} }, presets), ctx())
  const fs = compile(m).shader.fragmentShader
  assert.match(fs, /bisRimL = mix\( 0\.1, 1\.0, smoothstep\( 0\.05, 0\.7, bisLum \) \)/) // subdued rim
  assert.match(fs, /mix\( 0\.35, 1\.0, clamp\( bisLum \/ 0\.5, 0\.0, 1\.0 \) \)/) // subdued coat / specular
  assert.match(fs, /mix\( bisPm, vec3\( 1\.0 \), 0\.3 \* bisLum \)/) // glow carries no white for dark paints
  assert.match(fs, /mix\( 1\.0 - bisLgEdgeDark, 1\.0, smoothstep\( 0\.0, 0\.6, bisE \) \)/)
  assert.match(fs, /material\.diffuseContribution \* bisTransTint/) // deep tint only on the transmitted light
})

test('glass plate over a rendition wallpaper uses the worker backdrop-glass model', () => {
  const spec = m3d.describeMaterial({ preset: 'frosted_glass', params: { tint: 0.4, frost: 0.42 } }, presets)
  const m = new m3d.IconMaterial()
  const behind = { map: null, color: new THREE.Color(1, 1, 1), space: 'canvas', tone: 'dark', backdropGlass: true }
  m3d.applyIconMaterial(m, spec, ctx({ fake: behind }))
  assert.ok('BIS_BACKDROP_GLASS' in m.defines)
  assert.ok(Math.abs(m.bis.bisBdScatter.value - (0.1 + 0.35 * 0.42) * 0.32) < 1e-9)
  assert.equal(m.bis.bisBdDesat.value, 0.45)
  m3d.applyIconMaterial(m, spec, ctx({ fake: { ...behind, tone: 'light' } }))
  assert.ok(Math.abs(m.bis.bisBdScatter.value - (0.1 + 0.35 * 0.42)) < 1e-9)
  assert.equal(m.bis.bisBdDesat.value, 0)
  assert.deepEqual(compile(m).warnings, [])
})

test('soft-alpha halo detection mirrors scene.soft_alpha_fraction', () => {
  const rgba = (alphas) => alphas.flatMap((a) => [255, 255, 255, a])
  // neon tube: solid core + wide soft glow -> halo card
  const neon = rgba([...Array(30).fill(255), ...Array(70).fill(80), ...Array(100).fill(0)])
  assert.ok(Math.abs(softAlpha.softAlphaFromRgba(neon) - 0.7) < 1e-9)
  assert.ok(softAlpha.softAlphaFromRgba(neon) > softAlpha.HALO_SOFT_MIN)
  // translucent shading overlay without a solid core (Find Device) -> no halo
  assert.equal(softAlpha.softAlphaFromRgba(rgba([...Array(95).fill(60), ...Array(5).fill(255)])), 0)
  // hard-edged opaque art -> no halo
  assert.ok(softAlpha.softAlphaFromRgba(rgba([...Array(95).fill(255), ...Array(5).fill(128)])) < softAlpha.HALO_SOFT_MIN)
})

// ------------------------------------------------------------------------------------------------ geometry
const pt = (x, y) => ({ co: [x, y], hl: [x, y], hr: [x, y] })
const spline = (pts, extra = {}) => ({ closed: true, hole: false, parent: -1, depth: 0, points: pts.map(([x, y]) => pt(x, y)), ...extra })
const polyArea = (p) => {
  let a = 0
  const n = p.length / 2
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n
    a += p[2 * i] * p[2 * j + 1] - p[2 * j] * p[2 * i + 1]
  }
  return a / 2
}
function capArea(geo) {
  const pos = geo.attributes.position.array
  const idx = geo.index.array
  const T = geo.userData.thickness
  let a = 0
  for (let i = 0; i < idx.length; i += 3) {
    const [p, q, r] = [idx[i], idx[i + 1], idx[i + 2]]
    if ([p, q, r].every((v) => Math.abs(pos[3 * v + 2] - T) < 1e-6))
      a += ((pos[3 * q] - pos[3 * p]) * (pos[3 * r + 1] - pos[3 * p + 1]) - (pos[3 * r] - pos[3 * p]) * (pos[3 * q + 1] - pos[3 * p + 1])) / 2
  }
  return a
}

test('fills are always closed (an open subpath ending in h0 is filled, not swept as a tube)', () => {
  const open = spline([[0, 0], [1, 0], [1, 1], [0, 1]], { closed: false })
  const [s] = fillet.sanitizeSplines([open])
  assert.equal(s.closed, true)
  assert.deepEqual(s.points[0].hl, [0, 1 / 3]) // straight closing segment, handles at thirds
  const [f] = fillet.prepareSplines([open], 0.05)
  assert.equal(f.closed, true)
  assert.ok(f.points.length > 4, 'an open-flagged fill is prepared (filleted) like a closed one')
})

test('only convex corners are filleted: the silhouette never grows into concave corners', () => {
  // an L: one concave corner at (0.5, 0.5)
  const L = spline([[0, 0], [1, 0], [1, 0.5], [0.5, 0.5], [0.5, 1], [0, 1]])
  const [f] = fillet.filletCorners([L], 0.1)
  assert.ok(f.points.some((p) => Math.abs(p.co[0] - 0.5) < 1e-9 && Math.abs(p.co[1] - 0.5) < 1e-9), 'concave corner kept sharp')
  const area = (s) => polyArea(flatten.splinesToGroups([s])[0].outer.pts)
  assert.ok(area(f) < area(L), 'convex fillets only remove material')
  // the old behaviour (all corners) would add material at the concave corner
  const [all] = fillet.filletCorners([L], 0.1, fillet.FILLET_MIN_TURN, 0, false)
  assert.ok(!all.points.some((p) => Math.abs(p.co[0] - 0.5) < 1e-9 && Math.abs(p.co[1] - 0.5) < 1e-9))
})

test('collinear runs are joined before filleting', () => {
  const sq = spline([[0, 0], [0.3, 0], [0.6, 0.0000001], [1, 0], [1, 1], [0, 1]])
  const [m] = fillet.mergeCollinear([sq])
  assert.equal(m.points.length, 4)
})

test('an unfilletable acute convex corner takes the unfilleted (GN) route', () => {
  // a thin wedge: its 160° tip cannot take a 1.2 × bevel fillet
  const wedge = spline([[0, 0], [1, 0.05], [1, 0.1]])
  const out = fillet.prepareSplines([wedge], 0.05)
  assert.equal(out[0].points.length, 3)
})

test('pill caps of a grid with nearly touching inset holes triangulate correctly (Sheets)', () => {
  // Sheets' white grid: chamfered outer + 6 cells; the cap inset (0.042) leaves 0.003 between neighbouring cells
  const outer = spline([[-0.458, 0.375], [-0.418, 0.415], [0.418, 0.415], [0.458, 0.375], [0.458, -0.375], [0.418, -0.415], [-0.418, -0.415], [-0.458, -0.375]])
  const cells = []
  for (const [x0, x1] of [[-0.342, -0.058], [0.058, 0.342]])
    for (const [y0, y1] of [[-0.3, -0.158], [-0.071, 0.071], [0.158, 0.3]])
      cells.push(spline([[x1, y0], [x1, y1], [x0, y1], [x0, y0]], { hole: true, parent: 0, depth: 1 }))
  const splines = [outer, ...cells]
  const bevel = 0.045 / 1.072957
  const groups = flatten.splinesToGroups(fillet.prepareSplines(splines, bevel))
  const geo = pill.buildPillGeometry(groups, { thickness: 0.1 / 1.072957, bevel, segments: 6 })
  const raw = flatten.splinesToGroups(splines)[0]
  const solid = polyArea(raw.outer.pts) - raw.holes.reduce((a, h) => a + Math.abs(polyArea(h.pts)), 0)
  const cap = capArea(geo)
  assert.ok(cap > 0.08 && cap < 0.16, `cap area ${cap} (the mis-triangulated cap covered 0.41, filling the cells)`)
  assert.ok(cap < solid)
})

test('a sliver thinner than twice the bevel never sticks out of its outline (Translate fold tip)', () => {
  // the G card + fold of Translate (L3 e5): a 0.02-wide spike at the fold tip with a 0.042 bevel
  const pts = [[0.238, -0.294], [0.219, -0.378], [0.215, -0.382], [0.199, -0.342], [-0.46, -0.342], [-0.542, -0.26], [-0.542, 0.464], [-0.46, 0.546], [-0.111, 0.546], [-0.033, 0.491]]
  const s = spline(pts)
  const bevel = 0.0419
  const geo = pill.buildPillGeometry(flatten.splinesToGroups(fillet.prepareSplines([s], bevel)), { thickness: 0.093, bevel, segments: 6 })
  geo.computeBoundingBox()
  const b = geo.boundingBox
  assert.ok(b.max.x <= 0.238 + 0.01 && b.min.y >= -0.382 - 0.01, `mesh bbox ${JSON.stringify(b)} (the miter spike reached x 0.31)`)
  assert.ok(capArea(geo) > 0, 'front cap faces the camera')
})

test('untangleInset collapses a folded inset loop', () => {
  const outline = [0, 0, 1, 0, 1.01, 0, 1.02, 0, 2, 0, 2, 1, 0, 1]
  const pts = [[0.1, 0.1], [1.05, 0.1], [0.98, 0.1], [1.07, 0.1], [1.9, 0.1], [1.9, 0.9], [0.1, 0.9]].map(([x, y]) => new THREE.Vector2(x, y))
  const { keep, rep } = pill.untangleInset(outline, pts)
  for (let k = 0; k < keep.length; k++) {
    const i = keep[k]
    const j = keep[(k + 1) % keep.length]
    const dot = (outline[2 * j] - outline[2 * i]) * (pts[j].x - pts[i].x) + (outline[2 * j + 1] - outline[2 * i + 1]) * (pts[j].y - pts[i].y)
    assert.ok(dot >= 0, 'no reversed edge left')
  }
  assert.equal(rep.length, pts.length)
  assert.ok(keep.length < pts.length)
})

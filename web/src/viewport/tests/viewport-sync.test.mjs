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
const { neutralToneMap: neutral } = await import('../scene/displayTransform.ts')
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

// ------------------------------------------------------------------------------------------------ round 4: colour fidelity
test('colour calibration constants mirror the worker (display_paint, _albedo, ENGINE_CAL)', () => {
  const mat = read('blender_worker/materials.py')
  const util = read('blender_worker/util.py')
  assert.equal(m3d.DIFFUSE_A, pyConst(mat, 'DIFFUSE_A'))
  assert.equal(m3d.DIFFUSE_B, pyConst(mat, 'DIFFUSE_B'))
  assert.equal(m3d.ALBEDO_MAX, pyConst(mat, 'ALBEDO_MAX'))
  assert.equal(m3d.LG_COAT_B, pyConst(mat, 'LG_COAT_B'))
  assert.equal(m3d.PBR_START, pyConst(util, 'PBR_START'))
  assert.equal(m3d.PBR_DESAT, pyConst(util, 'PBR_DESAT'))
  const cap = /^NEUTRAL_CAP\s*=\s*\(([-0-9.]+),\s*([-0-9.]+)\)/m.exec(util)
  assert.deepEqual(m3d.NEUTRAL_CAP, [Number(cap[1]), Number(cap[2])])
  assert.equal(m3d.NEUTRAL_CAP_POW, pyConst(util, 'NEUTRAL_CAP_POW'))
  assert.match(util, /sat \*\* NEUTRAL_CAP_POW/)
  // the B offset each diffuse preset hands to _albedo(display_paint(col), B)
  const body = (name) => new RegExp(`def ${name}\\([\\s\\S]*?\\n(?=def |# =)`).exec(mat)[0]
  /** B argument of the preset's `diffuse_paint(c, <paint>[, B])` call (default DIFFUSE_B), by matching parentheses. */
  const bOf = (src) => {
    const i = src.indexOf('diffuse_paint(c, ')
    assert.ok(i >= 0, 'no diffuse_paint(...) call')
    const args = ['']
    for (let k = i + 'diffuse_paint('.length, depth = 1; depth > 0; k++) {
      const ch = src[k]
      depth += ch === '(' ? 1 : ch === ')' ? -1 : 0
      if (depth === 1 && ch === ',') args.push('')
      else if (depth > 0) args[args.length - 1] += ch
    }
    return args.length > 2 ? Number(args[2].trim()) : m3d.DIFFUSE_B
  }
  assert.match(body('diffuse_paint'), /return _albedo\(c, display_paint\(c, col, DIFFUSE_PEAK\), b\)/)
  assert.match(mat, /^DIFFUSE_PEAK = 1\.0 - \(1\.0 - PBR_START\) \*\* 2 \/ \(ALBEDO_MAX \+ DIFFUSE_B - \(2 \* PBR_START - 1\.0\)\)/m)
  assert.ok(Math.abs(m3d.DIFFUSE_PEAK - (1 - (1 - m3d.PBR_START) ** 2 / (m3d.ALBEDO_MAX + m3d.DIFFUSE_B - (2 * m3d.PBR_START - 1)))) < 1e-15)
  // a white target capped at DIFFUSE_PEAK un-compresses to the offset peak ALBEDO_MAX + B (+ the 0.04 toe offset)
  assert.ok(Math.abs(m3d.displayPaint([1, 1, 1], m3d.DIFFUSE_PEAK)[0] - 0.04 - (m3d.ALBEDO_MAX + m3d.DIFFUSE_B)) < 1e-9)
  // b_satin: hue-tinted specular + the tinted reflection (DIFFUSE_B) taken off the albedo
  const satin = body('b_satin')
  assert.match(satin, /rad = display_paint\(c, col, DIFFUSE_PEAK\)/)
  assert.match(satin, /tint = g\.vmath\("SCALE", rad, None, scale=g\.math\("DIVIDE", 1\.0, g\.math\("MAXIMUM", mx, 1e-4\)\)\)/)
  assert.match(satin, /_albedo\(c, g\.vmath\("SUBTRACT", rad, g\.vmath\("SCALE", tint, None, scale=DIFFUSE_B\)\), 0\.0\)/)
  assert.match(satin, /"Specular Tint": tint/)
  assert.equal(m3d.ALBEDO_B.satin, m3d.DIFFUSE_B)
  assert.equal(m3d.describeMaterial({ preset: 'satin', params: {} }, presets).specularTint, true)
  const files = m3d.satinPaint([0.807, 0.279, 0]) // Files #e89000-like: the zero channel stays zero
  assert.equal(files.albedo[2], 0)
  assert.ok(Math.abs(Math.max(...files.specularTint) - 1) < 1e-12 && files.specularTint[2] === 0)
  assert.equal(m3d.ALBEDO_B.glossy_plastic, bOf(body('b_glossy_plastic')))
  assert.equal(m3d.ALBEDO_B.candy, bOf(body('b_candy')))
  assert.equal(m3d.ALBEDO_B.gummy, bOf(body('_gummy')))
  assert.equal(m3d.ALBEDO_B.matte_clay, bOf(body('b_matte_clay')))
  assert.match(body('b_flat'), /paint_radiance\(c, col\)/)
  // paint_radiance: translucent pieces use paint + 0.04 (peak ≤ 1) instead of the exact inverse
  assert.match(body('paint_radiance'), /return _albedo\(c, col, -0\.04, 1\.0\)/)
  assert.deepEqual(m3d.paintRadiance([0.5, 0.2, 0], true).map((v) => +v.toFixed(6)), [0.54, 0.24, 0.04])
  assert.deepEqual(m3d.paintRadiance([0.5, 0.2, 0], false), m3d.displayPaint([0.5, 0.2, 0]))
  assert.deepEqual(m3d.paintRadiance([0.5, 0.2, 0], false, false), [0.5, 0.2, 0])
  for (const id of Object.keys(m3d.ALBEDO_B)) assert.equal(m3d.describeMaterial({ preset: id, params: {} }, presets).albedoB, m3d.ALBEDO_B[id])
  assert.equal(m3d.describeMaterial({ preset: 'flat', params: {} }, presets).displayEmission, true)
  // ENGINE_CAL calibrates Blender's rigs to the DIFFUSE_A · albedo + DIFFUSE_B response the paints are compensated for;
  // the live rig is calibrated to the same target by LIVE_CAL (rig.ts), applied to every light and the environment.
  assert.match(read('blender_worker/lighting.py'), /^ENGINE_CAL\s*=\s*\{"CYCLES":/m)
  assert.ok(rig.LIVE_CAL > 0.9 && rig.LIVE_CAL < 1.3)
  const studio = read('web/src/viewport/scene/StudioLighting.tsx')
  assert.match(studio, /scene\.environmentIntensity = LIVE_CAL/)
  assert.equal((studio.match(/DIFFUSE_CAL \* LIVE_CAL \* LIVE_LIGHT_CAL\.(key|fill) \* rig\./g) ?? []).length, 2) // key + fill
})

test('displayPaint is util.pbr_neutral_inverse: Khronos PBR Neutral shows the paint (peaks capped by saturation)', () => {
  const disp = (c) => neutral(m3d.displayPaint(c))
  // reference values from blender_worker/util.pbr_neutral_inverse (round 4)
  const ref = [
    [[1, 1, 1], [2.864, 2.864, 2.864]],
    [[0.8, 0.8, 0.8], [0.848, 0.848, 0.848]],
    [[0.215, 0.215, 0.215], [0.255, 0.255, 0.255]],
    [[0.046, 0.046, 0.046], [0.086, 0.086, 0.086]],
    [[0.807, 0.065, 0.019], [0.854061, 0.100245, 0.053512]],
    [[0.024, 0.125, 0.708], [0.061968, 0.162968, 0.745968]],
    [[0.964, 0.5, 0.0015], [0.937782, 0.481279, 0]],
  ]
  for (const [c, want] of ref) m3d.displayPaint(c).forEach((v, i) => assert.ok(Math.abs(v - want[i]) < 2e-6, `${c}: ${v} vs ${want[i]}`))
  // below the caps the round trip is exact; white is shown at the neutral cap (≈ 252/255)
  for (const c of [[0.5, 0.5, 0.5], [0.215, 0.215, 0.215], [0.6, 0.2, 0.05], [0.05, 0.3, 0.7]]) {
    disp(c).forEach((v, i) => assert.ok(Math.abs(v - c[i]) < 1e-6, `${c} → ${disp(c)}`))
  }
  disp([1, 1, 1]).forEach((v) => assert.ok(Math.abs(v - m3d.NEUTRAL_CAP[0]) < 1e-6))
  // display_paint's max_peak (diffuse presets: DIFFUSE_PEAK) caps the target below the saturation cap
  const w = m3d.displayPaint([1, 1, 1], m3d.DIFFUSE_PEAK)
  assert.ok(Math.abs(neutral(w)[0] - m3d.DIFFUSE_PEAK) < 1e-6)
  assert.ok(Math.abs(Math.max(...m3d.diffusePaint([1, 1, 1])) - m3d.ALBEDO_MAX) < 1e-6) // white plate: albedo 1.3
  // diffuse presets: the albedo the rig (DIFFUSE_A · albedo + B) lights to the radiance, hue kept at ALBEDO_MAX
  assert.deepEqual(m3d.diffuseAlbedo([0.5, 0.25, 0.017], 0.017).map((v) => +v.toFixed(6)), [0.483, 0.233, 0])
  const capped = m3d.diffuseAlbedo([2.48, 1.24, 0.62], 0.017)
  assert.ok(Math.abs(Math.max(...capped) - m3d.ALBEDO_MAX) < 1e-9 && Math.abs(capped[0] / capped[1] - 2.463 / 1.223) < 1e-3)
})

test('Liquid Glass round-4 model mirrors the worker (clear share, body radiance, edge thinning, clear tint)', () => {
  const src = read('blender_worker/materials.py')
  const lg = /def b_liquid_glass[\s\S]*?return cyc, ev, None/.exec(src)[0]
  assert.match(lg, /t_cap = min\(0\.6, LG_CAP_CLEAR \* \(max\(0\.0, transl\) \/ 0\.75\) \*\* 4\)/)
  assert.equal(m3d.LG_CAP_CLEAR, pyConst(src, 'LG_CAP_CLEAR'))
  assert.match(lg, /g\.map_range\(white, 0\.0, 1\.0, t_cap, 0\.35 \* t_cap\)/)
  assert.match(lg, /g\.map_range\(v01, 0\.0, 1\.0, 0\.94, 1\.0\)/)
  assert.match(lg, /g\.map_range\(e, 0\.0, 0\.45, LG_EDGE_BODY, 1\.0, interp="SMOOTHSTEP"\)/)
  assert.equal(m3d.LG_EDGE_BODY, pyConst(src, 'LG_EDGE_BODY'))
  // rim × lightness: dark paints keep only a faint sheen
  const rimL = /_rim_lg\(c, 5\.0 \* rim_amt, mode\),\s*g\.map_range\(lum, ([0-9.]+), ([0-9.]+), ([0-9.]+), ([0-9.]+), interp="SMOOTHSTEP"\)\)/.exec(lg)
  assert.ok(rimL, 'rim lightness map_range not found')
  assert.deepEqual([...m3d.LG_RIM_LIGHTNESS.from, ...m3d.LG_RIM_LIGHTNESS.to], rimL.slice(1, 5).map(Number))
  // translucent pieces: the clear share is partly untinted (their alpha already shows what lies beneath)
  assert.match(lg, /if c\.spec\.get\("alpha"\):[\s\S]*?t2 = g\.mix_rgb\(TRANSLUCENT_CLEAR, t2, WHITE\)/)
  assert.equal(m3d.TRANSLUCENT_CLEAR, pyConst(src, 'TRANSLUCENT_CLEAR'))
  // clear glass: white paints become frosted "ice" (worker _glass_common white_milk = CLEAR_WHITE_MILK)
  assert.match(src, /def b_clear_glass[\s\S]*?white_milk=CLEAR_WHITE_MILK\)/)
  assert.match(src, /elif white_milk > 0:[\s\S]*?wm = g\.math\("MULTIPLY", g\.math\("POWER", _whiteness\(c, col\), 1\.5\), white_milk\)\s*\n\s*milk = g\.math\("MAXIMUM", milk, g\.math\("MULTIPLY", wm, g\.map_range\(fall, 0\.45, 1\.0, 0\.8, 1\.0\)\)\)/)
  assert.equal(m3d.CLEAR_WHITE_MILK, pyConst(src, 'CLEAR_WHITE_MILK'))
  const cg = m3d.describeMaterial({ preset: 'clear_glass', params: {} }, presets)
  assert.equal(cg.whiteMilk, m3d.CLEAR_WHITE_MILK)
  const mcg = new m3d.IconMaterial()
  m3d.applyIconMaterial(mcg, cg, ctx())
  assert.ok('BIS_WHITE_MILK' in mcg.defines)
  assert.equal(mcg.bis.bisWhiteMilk.value, m3d.CLEAR_WHITE_MILK)
  const cfs = compile(mcg)
  assert.deepEqual(cfs.warnings, [])
  assert.match(cfs.shader.fragmentShader, /m = max\( m, pow\( bisWhiteness\( paint \), 1\.5 \) \* bisWhiteMilk \* mix\( 0\.8, 1\.0, h \) \);/)
  // round 5: frosted (WHITE_ICE) and prism (CLEAR_WHITE_MILK) white glyphs are frosted ice too (worker _white_ice)
  assert.equal(m3d.describeMaterial({ preset: 'frosted_glass', params: {} }, presets).whiteMilk, m3d.WHITE_ICE)
  assert.match(lg, /g\.map_range\(e, 0\.0, 0\.6, 0\.9, 0\.5\)/) // clear tint gamma per interface ↔ LG_DEEP
  assert.deepEqual(m3d.LG_DEEP, [0.9, 0.5])
  assert.match(lg, /mix_shader\(0\.85, pc\.outputs\[0\], fill_em\.outputs\[0\]\)/) // diffuse share 0.15
  assert.equal(m3d.LG_FILL_DIFFUSE, 0.15)
  assert.match(lg, /0\.95 \* lit \* face, 1\.04 \* lit \* face/)
  assert.match(lg, /c\.prm\("tint", 1\.0\)/)
  assert.match(lg, /rad = paint_radiance\(c, base_m\)/)
  assert.equal(presets.materials.liquid_glass.params.tint.default, 1)
  // defaults: LG_CAP_CLEAR clear share at translucency 0.75 (a third of it for white glyphs)
  const s = m3d.describeMaterial({ preset: 'liquid_glass', params: {} }, presets)
  const cap = m3d.LG_CAP_CLEAR
  assert.ok(Math.abs(s.lg.fill[0] - (1 - cap)) < 1e-9 && Math.abs(s.lg.fill[1] - (1 - 0.35 * cap)) < 1e-9)
  assert.equal(s.paintMix, 1) // tint 1 = the exact paint
  assert.ok(Math.abs(m3d.liquidGlassClearShare(1) - Math.min(0.6, cap / 0.75 ** 4)) < 1e-12)
  assert.equal(m3d.liquidGlassClearShare(2), 0.6)
  // shader: the body emits the displayed paint minus the coat reflection; the clear share is t2^(2γ)
  const m = new m3d.IconMaterial()
  m3d.applyIconMaterial(m, s, ctx({ displayPaint: true }))
  assert.ok('BIS_DISPLAY_PAINT' in m.defines)
  const { shader, warnings } = compile(m)
  assert.deepEqual(warnings, [])
  const fs = shader.fragmentShader
  assert.match(fs, /bisRad = bisPaintRadiance\( bisPm \);/)
  assert.match(fs, /max\( bisRad - 0\.01, vec3\( 0\.0 \) \) \* bisFill \* 0\.8500 \* mix\( 0\.95, 1\.04, bisV01 \)/)
  assert.match(fs, /vec3 bisT2 = min\( bisRad \/ 1\.3, vec3\( 1\.0 \) \);/)
  assert.ok(fs.includes(`mix( ${m3d.LG_EDGE_BODY}, 1.0, smoothstep( 0.0, 0.45, bisE ) )`))
  assert.ok(fs.includes(`float bisRimL = mix( ${m3d.LG_RIM_LIGHTNESS.to[0]}, 1.0, smoothstep( ${m3d.LG_RIM_LIGHTNESS.from[0]}, ${m3d.LG_RIM_LIGHTNESS.from[1]}, bisLum ) );`))
  assert.ok(fs.includes(`bisT2 = mix( bisT2, vec3( 1.0 ), ${m3d.TRANSLUCENT_CLEAR} );`))
  assert.ok(!/bisT2 = mix\( bisT2/.test(fs.replace(/#ifdef BIS_SRGB_ALPHA[\s\S]*?#endif/, '')), 'alpha untint must be gated')
  const ma = new m3d.IconMaterial() // a translucent Liquid Glass piece compiles with the untinted clear share
  m3d.applyIconMaterial(ma, s, ctx({ displayPaint: true, opacity: 0.6 }))
  assert.ok('BIS_SRGB_ALPHA' in ma.defines)
  assert.deepEqual(compile(ma).warnings, [])
  m3d.applyIconMaterial(m, s, ctx({ displayPaint: false }))
  assert.ok(!('BIS_DISPLAY_PAINT' in m.defines)) // other colour modes: identity (worker cm != 'neutral')
})

test('diffuse presets take the albedo of the displayed paint; flat emits it', () => {
  const m = new m3d.IconMaterial()
  m3d.applyIconMaterial(m, m3d.describeMaterial({ preset: 'satin', params: {} }, presets), ctx({ displayPaint: true }))
  assert.ok('BIS_ALBEDO' in m.defines && 'BIS_DISPLAY_PAINT' in m.defines && 'BIS_SPEC_TINT' in m.defines)
  assert.equal(m.bis.bisAlbedoB.value, m3d.DIFFUSE_B)
  const sfs = compile(m).shader.fragmentShader
  assert.match(sfs, /diffuseColor\.rgb \*= bisAlbedo\( bisSRad - bisSpecTint \* bisAlbedoB, 0\.0 \);/)
  assert.match(sfs, /material\.specularColor \*= bisSpecTint;/)
  const gp = new m3d.IconMaterial()
  m3d.applyIconMaterial(gp, m3d.describeMaterial({ preset: 'glossy_plastic', params: {} }, presets), ctx({ displayPaint: true }))
  assert.ok(!('BIS_SPEC_TINT' in gp.defines))
  assert.match(compile(gp).shader.fragmentShader, /diffuseColor\.rgb \*= bisAlbedo\( bisDisplayPaint\( bisPm, 0\.9277\d* \), bisAlbedoB \) \* bisAlbedoGain;/)
  const f = new m3d.IconMaterial()
  m3d.applyIconMaterial(f, m3d.describeMaterial({ preset: 'flat', params: {} }, presets), ctx({ displayPaint: true }))
  assert.ok('BIS_DISPLAY_EMISSION' in f.defines)
  assert.deepEqual(compile(f).warnings, [])
  const c = new m3d.IconMaterial()
  m3d.applyIconMaterial(c, m3d.describeMaterial({ preset: 'chrome', params: {} }, presets), ctx({ displayPaint: true }))
  assert.ok(!('BIS_ALBEDO' in c.defines)) // metals keep their paint as-is (worker b_chrome)
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
  // round 5: a smoky pane — CLEAR_LIGHT_PLATE × CLEAR_LIGHT_SMOKE in linear light (worker _scale_hex: '#787a80')
  assert.equal(p.canvas.plate.fill.color, '#787a80')
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
  // round 5: a weaker coat (CLEAR_DARK_COAT) and IOR 1.3 on the dark pane
  assert.deepEqual(p.canvas.plate.material, { preset: 'frosted_glass', params: { tint: 0, frost: 0.42, grain: 0.04, coat: 0.3, ior: 1.3 } })
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
  assert.match(fs, /bisRimL = mix\( 0\.06, 1\.0, smoothstep\( 0\.1, 0\.8, bisLum \) \)/) // subdued rim (worker 11:31)
  assert.match(fs, /mix\( 0\.35, 1\.0, clamp\( bisLum \/ 0\.5, 0\.0, 1\.0 \) \)/) // subdued coat / specular
  assert.match(fs, /mix\( bisPm, vec3\( 1\.0 \), 0\.3 \* bisLum \* \( 1\.0 - bisPmSat \) \)/) // glow: no white for dark / saturated paints
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

// ------------------------------------------------------------------------------------------------ round 5 (ui-sync)
// The worker's round-5 changes, mirrored: 'brand' colour mode (Standard + highlight soft clip, paints pre-compensated by
// the exact inverse, default for a missing mode), key light at KEY_DIST, Liquid Glass rim / near-black / flush / thin
// strokes / glow, display-space blend films for translucent pieces (overlay.py), clear / tinted mono maps (gamma / rank
// LUTs), smoky clear-light pane, weak-coat clear-dark pane, white ice on frosted / prism glass, merged touching pieces.
// Numbers marked "worker reference" were produced by the worker's own pure-Python functions (util / overlay /
// appearance) on the current sources; the constants are parsed from those sources, so a worker change fails here.
const dt = await import('../scene/displayTransform.ts')
const ov = await import('../../lib/overlay3d.ts')
const lg3 = await import('../geometry/layerGeometry.ts')
const cmodes = await import('../../lib/colorModes.ts')

/** `NAME = (a, b, c, d)` tuple → numbers. */
function pyTuple(src, re) {
  const m = re.exec(src)
  assert.ok(m, `${re} not found`)
  return m[1].split(',').map((v) => Number(v.trim()))
}
const close = (a, b, tol, msg) => assert.ok(Math.abs(a - b) <= tol, `${msg ?? ''} ${a} vs ${b}`)
const closeV = (a, b, tol, msg) => a.forEach((v, i) => close(v, b[i], tol, `${msg ?? ''}[${i}]`))

test("round 5: 'brand' colour mode = Standard + the worker's highlight soft clip (util.BRAND_KNEE / BRAND_CAP)", () => {
  const util = read('blender_worker/util.py')
  assert.equal(dt.BRAND_KNEE, pyConst(util, 'BRAND_KNEE'))
  assert.equal(dt.BRAND_CAP, pyConst(util, 'BRAND_CAP'))
  assert.equal(presets.colorModes.brand.softClip, dt.BRAND_KNEE)
  assert.match(util, /return tuple\(v if v <= knee else 1\.0 - w \* math\.exp\(-\(v - knee\) \/ w\) for v in/)
  assert.match(util, /out\.append\(y if y <= knee else knee - w \* math\.log\(1\.0 - \(y - knee\) \/ w\)\)/)
  // worker reference: util.soft_clip / util.soft_clip_inverse
  closeV(dt.softClip([0.5, 0.95, 1.3]), [0.5, 0.939347, 0.998168], 1e-6, 'soft_clip')
  closeV(dt.softClip([0.9, 2.0, 0.1]), [0.9, 0.999998, 0.1], 1e-6, 'soft_clip')
  closeV(dt.softClipInverse([0.5, 0.95, 0.998]), [0.5, 0.969315, 1.291202], 1e-6, 'soft_clip_inverse')
  closeV(dt.softClipInverse([0.2, 0.91, 1.0]), [0.2, 0.910536, 1.291202], 1e-6, 'soft_clip_inverse')
  closeV(dt.softClipInverse([0, 0.9, 0.99]), [0, 0.9, 1.130259], 1e-6, 'soft_clip_inverse')
  // the default for a missing / unknown mode (presets.DEFAULT_COLOR_MODE)
  assert.equal(dt.DEFAULT_COLOR_MODE, /^DEFAULT_COLOR_MODE\s*=\s*"(\w+)"/m.exec(read('blender_worker/presets.py'))[1])
  assert.equal(dt.colorModeId(undefined), 'brand')
  assert.equal(dt.colorModeId('nonsense'), 'brand')
  assert.equal(dt.colorModeId('neutral'), 'neutral')
  assert.deepEqual(dt.COLOR_MODES.slice().sort(), Object.keys(presets.colorModes).sort())
  // display transform + its inverse (backdrop) round-trip; the tone-mapping effect is the soft clip
  const t = dt.displayTransformFor('brand')
  assert.equal(t.mode, 3)
  for (const c of [[0.2, 0.5, 0.95], [0.004, 0.01, 0.02]]) closeV(dt.toDisplay(dt.toScene(c, t), t), c, 1e-9, 'round trip')
  assert.ok(dt.DISPLAY_INVERSE_GLSL.includes('bisToneMode == 3'))
  const fx = read('web/src/viewport/scene/Effects.tsx')
  assert.match(fx, /colorMode === 'brand' \? <primitive object=\{softClip\} \/> : <ToneMapping mode=\{mode\} \/>/)
  assert.match(fx, /const colorMode = colorModeId\(rawColorMode\)/)
  const sc = read('web/src/viewport/scene/softClipEffect.ts')
  assert.match(sc, /vec3 hi = 1\.0 - w \* exp\(-\(x - knee\) \/ w\);/)
  assert.match(sc, /mix\(x, hi, step\(vec3\(knee\), x\)\)/)
})

test("round 5: paints are pre-compensated with the exact inverse soft clip in 'brand' (materials._brand_graph)", () => {
  const mat = read('blender_worker/materials.py')
  assert.match(mat, /cap = BRAND_CAP if max_peak >= NEUTRAL_CAP\[0\] - 1e-9 else brand_diffuse_peak\(\)/)
  assert.match(mat, /return min\(BRAND_CAP, soft_clip\(\(ALBEDO_MAX \+ DIFFUSE_B,\), brand_knee\(\)\)\[0\]\)/)
  close(m3d.brandDiffusePeak(), Math.min(dt.BRAND_CAP, dt.softClip([m3d.ALBEDO_MAX + m3d.DIFFUSE_B, 0, 0])[0]), 1e-12)
  // opaque / glass bodies: targets ≤ BRAND_CAP; diffuse presets (DIFFUSE_PEAK requests): ≤ brand_diffuse_peak
  closeV(m3d.displayPaint([0.5, 0.95, 1.0], m3d.NEUTRAL_CAP[0], 'brand'), dt.softClipInverse([0.5, 0.95, 1.0]), 1e-12)
  closeV(m3d.displayPaint([1, 1, 1], m3d.DIFFUSE_PEAK, 'brand'), dt.softClipInverse([1, 1, 1], dt.BRAND_KNEE, m3d.brandDiffusePeak()), 1e-12)
  // paint_radiance: 'brand' uses the exact inverse for translucent pieces too (identity below the knee)
  assert.match(mat, /if not c\.spec\.get\("alpha"\) or c\.spec\.get\("cm", "neutral"\) != "neutral":\s*\n\s*return display_paint\(c, col\)/)
  closeV(m3d.paintRadiance([0.5, 0.2, 0], true, 'brand'), [0.5, 0.2, 0], 1e-12)
  assert.deepEqual(m3d.paintTransformFor('brand'), 'brand')
  assert.deepEqual(m3d.paintTransformFor(undefined), 'brand')
  assert.deepEqual(m3d.paintTransformFor('agx'), 'identity')
  // satin in 'brand': dark paints get a proportionally weaker specular and coat
  assert.equal(m3d.SATIN_DARK, pyConst(mat, 'SATIN_DARK'))
  assert.match(mat, /dk = g\.map_range\(mx, 0\.0, SATIN_DARK, 0\.12, 1\.0\)/)
  const dj = m3d.satinPaint([0.0052, 0.0052, 0.0052], 'brand') // #101010
  close(dj.coat, 0.12 + 0.88 * 0.0052 / m3d.SATIN_DARK, 1e-9)
  assert.equal(m3d.satinPaint([0.5, 0.2, 0.1], 'brand').coat, 1)
  // shader: BIS_BRAND_PAINT carries the same inverse
  const m = new m3d.IconMaterial()
  m3d.applyIconMaterial(m, m3d.describeMaterial({ preset: 'satin', params: {} }, presets), ctx({ displayPaint: 'brand' }))
  assert.ok('BIS_BRAND_PAINT' in m.defines && !('BIS_DISPLAY_PAINT' in m.defines))
  const fs = compile(m).shader.fragmentShader
  assert.deepEqual(compile(m).warnings, [])
  assert.match(fs, /vec3 hi = 0\.9 - 0\.100000 \* log\( 1\.0 - max\( y - 0\.9, vec3\( 0\.0 \) \) \/ 0\.100000 \);/)
  assert.match(fs, /bisSatinCoat = mix\( 0\.12, 1\.0, clamp\( bisSMx \/ 0\.08, 0\.0, 1\.0 \) \);/)
})

test('round 5: key light at lighting.KEY_DIST (same centre irradiance and angular size), fill as a near area light', () => {
  const src = read('blender_worker/lighting.py')
  const studio = read('web/src/viewport/scene/StudioLighting.tsx')
  assert.equal(Number(/export const KEY_DISTANCE = ([0-9.]+)/.exec(studio)[1]), pyConst(src, 'KEY_DIST'))
  assert.match(src, /^KEY_SCALE = KEY_DIST \/ 6\.0/m)
  assert.match(studio, /export const KEY_SCALE = KEY_DISTANCE \/ 6/)
  assert.match(src, /energy = K_BASE \* DIFFUSE_CAL \* rig\["key"\] \* KEY_SCALE \*\* 2/)
  assert.match(studio, /const KEY_POWER = 1\.9 \* KEY_DISTANCE \* KEY_DISTANCE/) // centre irradiance independent of d
  assert.match(studio, /const keySize = 4 \* KEY_SCALE \* \(0\.3 \+ 1\.4 \* rig\.softness\)/)
  const fillD = /\("BIS Fill", 160\.0, 55\.0, ([0-9.]+),/.exec(src)
  assert.equal(Number(/const FILL_DISTANCE = ([0-9.]+)/.exec(studio)[1]), Number(fillD[1]))
  // the world terms are linear in the environment, like the worker's _world_graph
  assert.match(src, /soft = g\.map_range\(g\.vmath\("DOT_PRODUCT", d, L\), 0\.90, 0\.97, 0\.0, 6\.0 \* max\(0\.2, rig\["key"\]\)/)
  assert.match(src, /front = g\.map_range\(g\.vmath\("DOT_PRODUCT", d, \(0\.0, 0\.0, 1\.0\)\), 0\.0, 1\.0, 0\.0, 0\.5 \* max\(0\.3, rig\["fill"\]\)\)/)
  assert.match(src, /g\.set\(bg\.inputs\["Strength"\], 0\.6 \* WORLD_CAL \* rig\["environment"\] \* strength_scale\)/)
  const env = read('web/src/viewport/scene/studioEnvironment.ts')
  assert.match(env, /domeU\.strength\.value = 0\.6 \* WORLD_CAL \* env \* LIVE_LIGHT_CAL\.dome/)
  assert.match(env, /this\.softbox\.set\(6 \* WORLD_CAL \* Math\.max\(0\.2, rig\.key\) \* env \* LIVE_LIGHT_CAL\.softbox, keyColor\)/)
  assert.match(env, /0\.5 \* Math\.max\(0\.3, rig\.fill\) \* LIVE_LIGHT_CAL\.front/)
  // per-component live calibration stays a calibration (measured, near 1 except the structural dome / key / fill)
  for (const [k, v] of Object.entries(rig.LIVE_LIGHT_CAL)) assert.ok(v > 0.4 && v < 1.6, `${k} ${v}`)
})

test('round 5: Liquid Glass rim band, near-black glass, flush edges, thin strokes, saturated glow, raster rim', () => {
  const mat = read('blender_worker/materials.py')
  const lgsrc = /def b_liquid_glass[\s\S]*?return cyc, ev, None/.exec(mat)[0]
  assert.deepEqual(m3d.LG_RIM_BANDS.auto, pyTuple(mat, /RIM_BANDS = \{"auto": \(([^)]*)\)/))
  assert.deepEqual(m3d.LG_RIM_BANDS.inside, pyTuple(mat, /RIM_BANDS = \{"auto": \([^)]*\), "inside": \(([^)]*)\)/))
  assert.deepEqual(m3d.LG_RIM_BANDS.outside, pyTuple(mat, /RIM_BANDS = \{[^}]*"outside": \(([^)]*)\)\}/))
  assert.deepEqual(m3d.LG_NEAR_BLACK, pyTuple(lgsrc, /nb = g\.map_range\(lum, ([0-9.]+, [0-9.]+), 0\.0, 1\.0\)/))
  assert.match(lgsrc, /rim = g\.math\("MULTIPLY", rim, nb\)/)
  assert.match(lgsrc, /rim = g\.math\("MULTIPLY", rim, keep\)/)
  assert.match(lgsrc, /fill = g\.math\("MAXIMUM", fill, g\.math\("SUBTRACT", 1\.0, keep\)\)/)
  assert.match(lgsrc, /fill = g\.math\("MAXIMUM", fill, g\.math\("SUBTRACT", 1\.0, nb\)\)/)
  assert.match(lgsrc, /fill = g\.math\("MAXIMUM", fill, float\(c\.spec\.get\("solid_edge"\) or 0\.0\)\)/)
  assert.match(lgsrc, /coat_b = g\.math\("MULTIPLY", coat_b, g\.mix_float\(nb, g\.map_range\(e, 0\.6, 0\.9, 0\.0, 0\.5\), 1\.0\)\)/)
  assert.match(lgsrc, /g\.math\("MULTIPLY", g\.math\("MULTIPLY", lum, 0\.3\),\s*g\.math\("SUBTRACT", 1\.0, hsv\.outputs\[1\]\)\), base_m, WHITE\)/)
  assert.match(lgsrc, /g\.set\(glow_em\.inputs\["Strength"\], g\.math\("MULTIPLY", gstr, keep\)\)/)
  const scene = read('blender_worker/scene.py')
  assert.equal(m3d.THIN_RATIO, pyConst(scene, 'THIN_RATIO'))
  assert.equal(m3d.THIN_SOLID, pyConst(scene, 'THIN_SOLID'))
  assert.equal(m3d.RASTER_RIM, pyConst(scene, 'RASTER_RIM'))
  assert.match(scene, /solid_edge=THIN_SOLID if bevel_local >= THIN_RATIO \* float\(g\.get\("safeRadius", 1\.0\)\) else 0\.0/)
  assert.equal(ov.solidEdge(0.04, 0.05), m3d.THIN_SOLID)
  assert.equal(ov.solidEdge(0.03, 0.05), 0)
  // shader
  const s = m3d.describeMaterial({ preset: 'liquid_glass', params: {} }, presets)
  const flush = { shape: 'squircle', r: 0.45, grow: 1, band: [0.0225, 0.0625] }
  const m = new m3d.IconMaterial()
  m3d.applyIconMaterial(m, s, ctx({ displayPaint: 'brand', flush, solidEdge: m3d.THIN_SOLID, rimScale: m3d.RASTER_RIM }))
  assert.ok('BIS_FLUSH' in m.defines)
  assert.equal(m.bis.bisLgSolidEdge.value, m3d.THIN_SOLID)
  close(m.bis.bisLgRim.value, s.lg.rim * m3d.RASTER_RIM, 1e-12)
  assert.deepEqual(m.bis.bisFlush.value.toArray(), [1, 0.45, 1, 0])
  const { shader, warnings } = compile(m)
  assert.deepEqual(warnings, [])
  const fs = shader.fragmentShader
  assert.match(fs, /float bisNb = clamp\( \( bisLum - 0\.04 \) \/ 0\.18\d*, 0\.0, 1\.0 \);/)
  assert.match(fs, /bisFill = max\( max\( bisFill, 1\.0 - bisKeep \), max\( 1\.0 - bisNb, bisLgSolidEdge \) \);/)
  assert.match(fs, /bisLgRim \* bisRimL \* bisNb \* bisKeep;/)
  assert.match(fs, /float bisCoatNb = mix\( clamp\( \( bisE - 0\.6 \) \/ 0\.3, 0\.0, 1\.0 \) \* 0\.5, 1\.0, bisNb \);/)
  assert.match(fs, /return smoothstep\( bisFlushBand\.x, bisFlushBand\.y, mix\( dR, dS, bisFlush\.z \) \* grow \);/)
})

test('round 5: flush-with-plate detection mirrors overlay.flush_spec / plate_distance', () => {
  const o = read('blender_worker/overlay.py')
  assert.equal(ov.FLUSH_TOL, pyConst(o, 'FLUSH_TOL'))
  assert.match(o, /"band": \(round\(0\.5 \* b, 5\), round\(1\.3 \* b \+ 0\.004, 5\)\)\}/)
  assert.match(o, /d = 1\.0 - \(x \*\* 5 \+ y \*\* 5\) \*\* 0\.2/)
  // worker reference: overlay.plate_distance(shape, 0.225, p)
  const ref = [
    ['squircle', [0.5, 0.5], 0.425651], ['squircle', [0.99, 0], 0.01], ['squircle', [0.9, 0.9], -0.033829],
    ['rounded', [0.5, 0.5], 0.5], ['rounded', [0.99, 0], 0.01], ['rounded', [0.9, 0.9], -0.044975],
    ['circle', [0.5, 0.5], 0.292893], ['circle', [0.9, 0.9], -0.272792], ['square', [0.9, 0.9], 0.1],
  ]
  for (const [shape, p, d] of ref) close(ov.plateDistance(shape, 0.225, p), d, 2e-6, `${shape} ${p}`)
  const ring = Array.from({ length: 80 }, (_, i) => [Math.cos((2 * Math.PI * i) / 80), Math.sin((2 * Math.PI * i) / 80)])
  assert.deepEqual(ov.flushSpec([ring], 'circle', 0.225, 0.045), { shape: 'circle', r: 0.45, grow: 1, band: [0.0225, 0.0625] })
  assert.equal(ov.flushSpec([ring.map(([x, y]) => [x * 0.5, y * 0.5])], 'circle', 0.225, 0.045), null)
  // the shader's field is the same function (squircle / rounded SDF in units of grow)
  const m = new m3d.IconMaterial()
  m3d.applyIconMaterial(m, m3d.describeMaterial({ preset: 'liquid_glass', params: {} }, presets), ctx({ flush: { shape: 'rounded', r: 0.45, grow: 1, band: [0.02, 0.06] } }))
  assert.deepEqual(m.bis.bisFlush.value.toArray(), [1, 0.45, 0, 0])
})

test('round 5: translucent Liquid Glass pieces are display-space blend films (overlay.blend_coeffs / beneath_srgb)', () => {
  const o = read('blender_worker/overlay.py')
  assert.equal(ov.FILM_FOLLOW, pyConst(o, 'FILM_FOLLOW'))
  assert.equal(ov.SAMPLES, pyConst(o, 'SAMPLES'))
  assert.deepEqual([...ov.FILM_MODES], /^FILM_MODES = \(([^)]*)\)/m.exec(o)[1].split(',').map((v) => v.trim().replace(/"/g, '')))
  // worker reference: overlay.blend_coeffs(paint, a, beneath, cm)
  const ref = [
    [[0, 0, 0], 0.33, [0.3, 0.43, 0.96], 'brand', [0.423753, 0.412565, 0.321312], [0.002382, 0.003589, 0.078245]],
    [[1, 1, 1], 0.66, [0.98, 0.56, 0.0], 'neutral', [1.0, 1.0, 1.0], [0.804484, 0.931743, 0.716121]],
    [[0.5, 0.5, 0.5], 0.5, [1, 1, 1], 'standard', [0.0, 0.0, 0.0], [0.522522, 0.522522, 0.522522]],
    [[0, 0, 0], 0.44, [0.58, 0.72, 0.0], 'brand', [0.272597, 0.268232, 0.56], [0.005517, 0.007176, 0.0]],
  ]
  for (const [p, a, b, cm, t, e] of ref) {
    const r = ov.blendCoeffs(p, a, b, cm)
    closeV(r.t, t, 2e-5, `T ${cm}`)
    closeV(r.e, e, 2e-5, `E ${cm}`)
  }
  // film_params: FILM_FOLLOW of the slope, a mid-grey estimate when nothing lies beneath
  const f0 = ov.filmParams([0, 0, 0], 0.33, null, 'brand')
  closeV(f0.t, [0.224802, 0.224802, 0.224802], 2e-5)
  closeV(f0.e, [0.043663, 0.043663, 0.043663], 2e-5)
  const f1 = ov.filmParams([0, 0, 0], 0.33, [0.3, 0.43, 0.96], 'brand')
  closeV(f1.t, [0.233064, 0.226911, 0.176722], 2e-5)
  closeV(f1.e, [0.016348, 0.032342, 0.210128], 2e-5)
  // beneath estimate: plate + lower layers + lower regions of the same layer, SVG-composited (worker reference)
  const sq = (x0, y0, x1, y1) => ({ closed: true, hole: false, parent: -1, depth: 0, points: [[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map((p) => ({ co: p, hl: p, hr: p })) })
  const lay = (id) => ({ id, visible: true, opacity: 1, fill: { type: 'auto' }, transform: { x: 0, y: 0, scale: 1 }, material: { preset: 'liquid_glass', params: {} }, mode: 'individual' })
  const proj = {
    canvas: { shape: 'rounded', cornerRadius: 0.225, art: { scale: 1, x: 0, y: 0 }, plate: { visible: true, fill: { type: 'solid', color: '#2050e0', opacity: 1 } } },
    layers: [lay('L1'), lay('L2')],
  }
  const geo = {
    L1: { regions: [{ elementId: 'a', opacity: 1, paint: { type: 'solid', color: '#ffffff' }, splines: [sq(-0.5, -0.5, 0.5, 0.5)] }] },
    L2: {
      regions: [
        { elementId: 'b', opacity: 0.8, paint: { type: 'solid', color: '#ff0000' }, splines: [sq(-0.2, -0.2, 0.3, 0.3)] },
        { elementId: 'c', opacity: 0.33, paint: { type: 'solid', color: '#000000' }, splines: [sq(0.0, -0.8, 0.8, 0.8)] },
      ],
    },
  }
  closeV(ov.beneathSrgb(proj, geo, 'L2', 1, 'rounded'), [0.527051, 0.526811, 0.832213], 1e-5, 'beneath')
  // which pieces film (scene._film_params): translucent solid pieces of non-combined Liquid Glass layers, light / dark only
  const films = ov.filmParamsFor(proj, geo, 'brand', false, lg3.touchingOpaque)
  assert.deepEqual([...films.keys()].sort(), ['L2:0', 'L2:1'])
  assert.equal(ov.filmParamsFor(proj, geo, 'agx', false, lg3.touchingOpaque).size, 0)
  assert.equal(ov.filmParamsFor(proj, geo, 'brand', true, lg3.touchingOpaque).size, 0) // clear / tinted renditions
  const grad = structuredClone(geo)
  grad.L2.regions[0].paint = { type: 'linear', start: [0, 0], end: [1, 0], stops: [{ offset: 0, color: '#000000', opacity: 1 }, { offset: 1, color: '#ffffff', opacity: 1 }] }
  assert.equal(ov.filmParamsFor(proj, grad, 'brand', false, lg3.touchingOpaque).size, 0) // a gradient piece blocks its layer
  // worker plumbing + the live shader: dst' = E + T · dst (blend colour T), no transmission, coat × alpha
  assert.match(read('blender_worker/materials.py'), /ec = g\.vmath\("MAXIMUM", g\.vmath\("SUBTRACT", e, g\.combine\(\*\(g\.math\("MULTIPLY", coat_b, LG_COAT_B\),\) \* 3\)\),/)
  const m = new m3d.IconMaterial()
  m3d.applyIconMaterial(m, m3d.describeMaterial({ preset: 'liquid_glass', params: {} }, presets), ctx({ displayPaint: 'brand', opacity: 0.33, film: f1 }))
  assert.ok(m.isFilm && 'BIS_FILM' in m.defines && !('BIS_SRGB_ALPHA' in m.defines))
  assert.equal(m.blending, THREE.CustomBlending)
  assert.equal(m.blendDst, THREE.ConstantColorFactor)
  closeV(m.blendColor.toArray(), f1.t, 1e-6)
  assert.equal(m.transmission, 0)
  assert.deepEqual(compile(m).warnings, [])
  assert.match(compile(m).shader.fragmentShader, /totalEmissiveRadiance \+= max\( bisFilmE - bisTame \* bisCoatNb \* bisFilmAlpha \* 0\.01, vec3\( 0\.0 \) \);/)
})

test('round 5: clear / tinted mono maps mirror appearance.gamma_lut / mono_lut (and the rendition rules)', () => {
  const src = read('blender_worker/appearance.py')
  for (const k of ['CLEAR_MONO_FLOOR', 'CLEAR_MONO_GAMMA', 'CLEAR_DARK_COAT', 'CLEAR_COMBINED_FLOOR', 'CLEAR_COMBINED_LINEAR',
    'CLEAR_LIGHT_SMOKE', 'TINT_LIGHT_GAIN', 'TINT_DARK_FLOOR', 'MONO_LINEAR', 'MONO_MERGE'])
    assert.equal(appearance[k], pyConst(src, k), k)
  assert.match(src, /"lut": gamma_lut\(vals, CLEAR_MONO_FLOOR, CLEAR_MONO_GAMMA\)\}/)
  assert.match(src, /"lut": mono_lut\(vals, CLEAR_COMBINED_FLOOR, linear=CLEAR_COMBINED_LINEAR\)\}/)
  assert.match(src, /"strength": min\(1\.0, strength \+ TINT_LIGHT_GAIN\), "lut": mono_lut\(vals, MONO_FLOOR\)\}/)
  assert.match(src, /mono=\{"lo": lo, "hi": hi, "floor": TINT_DARK_FLOOR, "tint": tint_lin, "strength": strength,\s*"lut": mono_lut\(vals, TINT_DARK_FLOOR\)\}/)
  // worker reference: appearance.mono_lut / gamma_lut
  const vals = [0.2, 0.21, 0.5, 0.53, 0.58, 0.9, 1.0, 0.05]
  const eq = (a, b, msg) => {
    assert.equal(a.length, b.length, `${msg}: ${JSON.stringify(a)}`)
    a.forEach(([x, y], i) => { close(x, b[i][0], 1e-6, msg); close(y, b[i][1], 1e-6, msg) })
  }
  eq(appearance.monoLut(vals, 0.3), [[0.05, 0.3], [0.2, 0.414825], [0.5, 0.562807], [0.53, 0.651105], [0.58, 0.743825], [0.9, 0.896228], [1.0, 1.0]], 'mono_lut')
  eq(appearance.monoLut(vals, 0.4, 0.55, 0.5), [[0.05, 0.4], [0.2, 0.497368], [0.5, 0.642105], [0.53, 0.701579], [0.58, 0.767368], [0.9, 0.918421], [1.0, 1.0]], 'mono_lut combined')
  eq(appearance.gammaLut(vals, 0.3, 0.35), [[0.05, 0.3], [0.0595, 0.439668], [0.0785, 0.505159], [0.1165, 0.575983], [0.1735, 0.642751], [0.2, 0.666882], [0.259, 0.712046], [0.3825, 0.784755], [0.5, 0.838914], [0.525, 0.849209], [0.53, 0.851225], [0.58, 0.870678], [0.715, 0.917849], [0.8575, 0.961294], [0.9, 0.973273], [1.0, 1.0]], 'gamma_lut')
  eq(appearance.monoLut([0.8, 0.85, 0.9], 0.5), [[0.35, 0.5], [0.8, 0.622727], [0.85, 0.811364], [0.9, 1.0]], 'mono_lut narrow')
  const many = Array.from({ length: 41 }, (_, i) => i / 40)
  const gm = appearance.gammaLut(many, 0.3, 0.35)
  assert.equal(gm.length, 30)
  close(gm[2][0], 0.025, 1e-9, 'thinned stop')
  close(gm[27][0], 0.925, 1e-9, 'thinned stop')
  const mm = appearance.monoLut(many, 0.3)
  assert.equal(mm.length, 30)
  close(mm[19][0], 0.65, 1e-9, 'banker-rounded thinning')
  // the colour ramp is piecewise linear and clamps at the end stops (Blender ValToRGB)
  close(appearance.lutValue([[0.2, 0.3], [0.6, 0.7]], 0.4), 0.5, 1e-12)
  assert.equal(appearance.lutValue([[0.2, 0.3], [0.6, 0.7]], 0.9), 0.7)
  // live: a LUT texture sampled like the ramp
  const lut = appearance.gammaLut(vals, 0.3, 0.35)
  const tex = m3d.monoLutTexture(lut)
  assert.equal(tex.image.width, m3d.MONO_LUT_SIZE)
  close(THREE.DataUtils.fromHalfFloat(tex.image.data[4 * 128]), appearance.lutValue(lut, 128 / 255), 2e-3)
  // the rendition picks: clear → gamma / rank (combined), tinted → rank, light / dark → none
  const luts = appearance.monoLutsFor('clear-light', [{ id: 'A', visible: true, fill: { type: 'solid', color: '#ffffff', opacity: 1 } }], {})
  assert.ok(luts && luts.mono.length && luts.combined.length)
  assert.equal(appearance.monoLutsFor('dark', [], {}), null)
  // tinted-light: mono × tint at min(1, strength + TINT_LIGHT_GAIN); the glass tint keeps the tint's own strength
  const p = appearance.resolveAppearance(project(), 'tinted-light')
  assert.equal(p.layers[0].material.params.__tintStrength, Math.min(1, 0.8 + appearance.TINT_LIGHT_GAIN))
  close(p.layers[0].material.params.tint, 0.55 + 0.4 * 0.8, 1e-12)
  // shader: BIS_MONO_LUT replaces the linear stretch
  const m = new m3d.IconMaterial()
  const intent = { ...m3d.describeMaterial(p.layers[0].material, presets) }
  m3d.applyIconMaterial(m, intent, ctx({ paint: { map: null, color: new THREE.Color(1, 1, 1), lumRange: [0, 1], monoLut: tex } }))
  assert.ok('BIS_MONO_LUT' in m.defines)
  assert.deepEqual(compile(m).warnings, [])
  assert.match(compile(m).shader.fragmentShader, /float st = texture2D\( bisMonoLut, vec2\( min\( l, 1\.0 \) \* 0\.99609375 \+ 0\.00195313, 0\.5 \) \)\.r;/)
})

test('round 5: white ice on frosted / prism glass (not the plate); glass transmits sqrt(body) like Cycles', () => {
  const mat = read('blender_worker/materials.py')
  assert.equal(m3d.WHITE_ICE, pyConst(mat, 'WHITE_ICE'))
  assert.match(mat, /cyc, ev = _white_ice\(c, col, normal, p\.outputs\[0\], ev, WHITE_ICE, frost\)/)
  assert.match(mat, /cyc, ev = _white_ice\(c, col, normal, cyc, ev, CLEAR_WHITE_MILK, frost\)/)
  assert.match(mat, /if amount <= 0 or c\.spec\.get\("plate"\) or c\.spec\.get\("clear"\):/)
  assert.equal(m3d.describeMaterial({ preset: 'dispersive_crystal', params: {} }, presets).whiteMilk, m3d.CLEAR_WHITE_MILK)
  const fr = m3d.describeMaterial({ preset: 'frosted_glass', params: {} }, presets)
  const m = new m3d.IconMaterial()
  m3d.applyIconMaterial(m, fr, ctx())
  assert.ok('BIS_WHITE_MILK' in m.defines)
  m3d.applyIconMaterial(m, fr, ctx({ plate: true }))
  assert.ok(!('BIS_WHITE_MILK' in m.defines), 'the plate never gets the white-ice body')
  // the worker mixes milk at both faces of a slab: (1 − m)² stays clear
  m3d.applyIconMaterial(m, fr, ctx())
  const fs = compile(m).shader.fragmentShader
  assert.match(fs, /return 1\.0 - \( 1\.0 - m \) \* \( 1\.0 - m \);/)
  assert.ok(fs.includes(`bisTransTint = ${m3d.GLASS_TRANSMIT} * sqrt( max( bisPm, vec3( 0.0 ) ) ) / max( bisPm, vec3( 1e-4 ) );`))
  assert.match(mat, /The transmission colour is sqrt\(body\)/)
})

test('round 5: touching opaque pieces render as one body (scene.touching_opaque)', () => {
  const scene = read('blender_worker/scene.py')
  assert.match(scene, /return 0 < sil_outer < reg_outer/)
  assert.match(scene, /combined_body = Lr\.get\("mode"\) == "combined" or touching_opaque\(g, images\)/)
  const sq = (x0, y0, x1, y1, hole = false) => ({ closed: true, hole, parent: hole ? 0 : -1, depth: hole ? 1 : 0, points: [[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map((p) => ({ co: p, hl: p, hr: p })) })
  const reg = (s, extra = {}) => ({ elementId: 'e', paint: { type: 'solid', color: '#ff0000', opacity: 1 }, opacity: 1, zSub: 0, splines: [s], ...extra })
  const two = { regions: [reg(sq(0, 0, 1, 1)), reg(sq(1, 0, 2, 1))], silhouette: [sq(0, 0, 2, 1)], images: [] }
  assert.equal(lg3.touchingOpaque(two), true)
  assert.equal(lg3.touchingOpaque({ ...two, silhouette: [sq(0, 0, 1, 1), sq(1.5, 0, 2, 1)] }), false) // separate pieces
  assert.equal(lg3.touchingOpaque({ ...two, regions: [two.regions[0], reg(sq(1, 0, 2, 1), { opacity: 0.5 })] }), false)
  assert.equal(lg3.touchingOpaque({ ...two, images: [{ elementId: 'x' }] }), false)
  const layer = { mode: 'individual', fill: { type: 'auto' } }
  assert.equal(lg3.layerBodyParts(layer, { layerId: 'L', hash: 'h', ...two }, { thickness: 0.1, bevel: 0.02, segments: 4, scale: 1 }).length, 1)
})

test('round 5: Render ▸ Colour offers every presets colour mode, Brand-exact marked as the default', () => {
  const opts = cmodes.colorModeOptions(presets)
  assert.deepEqual(opts.map((o) => o.value), Object.keys(presets.colorModes))
  const brand = opts.find((o) => o.value === 'brand')
  assert.equal(brand.label, `${presets.colorModes.brand.label} (default)`)
  assert.match(brand.description, /Standard \+ highlight soft clip/)
  assert.equal(opts.find((o) => o.value === 'neutral').label, presets.colorModes.neutral.label)
  assert.equal(cmodes.effectiveColorMode(undefined, presets), 'brand')
  assert.equal(cmodes.effectiveColorMode('agx', presets), 'agx')
  const ui = read('web/src/features/editor/inspector/RenderInspector.tsx')
  assert.match(ui, /options=\{colorModeOptions\(presets\)\}/)
  assert.doesNotMatch(ui, /Neutral \(Khronos PBR\) keeps brand colours accurate/)
})

test('round 5: cap triangulation survives self-crossing inset rings (Earth waves hairpins)', () => {
  const V = (x, y) => new THREE.Vector2(x, y)
  assert.equal(pill.ringsCross([[V(0, 0), V(1, 0), V(1, 1), V(0, 1)]]), false)
  assert.equal(pill.ringsCross([[V(0, 0), V(1, 1), V(1, 0), V(0, 1)]]), true) // bow tie
  // Earth's lowest wave (fixture): hairpins where it meets the plate edge fold the inset cap ring; earcut fanned a wedge
  // across the gap between the waves. The cap must stay inside the outline.
  const fx = JSON.parse(readFileSync(new URL('./fixtures/earth-wave.json', import.meta.url), 'utf8'))
  const S = 1.072959
  const bevel = Math.min(0.045 / S, 0.9 * fx.safeRadius, 0.1 / S / 2)
  const geo = pill.buildPillGeometry(flatten.splinesToGroups(fillet.prepareSplines([fx.spline], bevel)), { thickness: 0.1 / S, bevel, segments: 6 })
  const outline = flatten.splinesToGroups([fx.spline])[0].outer.pts
  const inPoly = (x, y, p) => {
    let c = false
    for (let i = 0, j = p.length / 2 - 1; i < p.length / 2; j = i++)
      if (p[2 * i + 1] > y !== p[2 * j + 1] > y && x < ((p[2 * j] - p[2 * i]) * (y - p[2 * i + 1])) / (p[2 * j + 1] - p[2 * i + 1]) + p[2 * i]) c = !c
    return c
  }
  const pos = geo.attributes.position.array
  const idx = geo.index.array
  const T = geo.userData.thickness
  const tris = []
  for (let i = 0; i < idx.length; i += 3) {
    const v = [idx[i], idx[i + 1], idx[i + 2]]
    if (v.every((k) => Math.abs(pos[3 * k + 2] - T) < 1e-6)) tris.push(v.map((k) => [pos[3 * k], pos[3 * k + 1]]))
  }
  const inTri = (x, y, [a, b, c]) => {
    const d = (p, q) => (q[0] - p[0]) * (y - p[1]) - (q[1] - p[1]) * (x - p[0])
    const s1 = d(a, b), s2 = d(b, c), s3 = d(c, a)
    return (s1 >= 0 && s2 >= 0 && s3 >= 0) || (s1 <= 0 && s2 <= 0 && s3 <= 0)
  }
  let covered = 0
  let stray = 0
  for (let gx = -0.95; gx <= 0.95; gx += 0.025)
    for (let gy = -0.95; gy <= 0.95; gy += 0.025) {
      if (!tris.some((t) => inTri(gx, gy, t))) continue
      covered++
      if (!inPoly(gx, gy, outline)) stray++
    }
  assert.ok(covered > 900, `cap covers ${covered} samples`)
  assert.ok(stray <= 0.01 * covered, `${stray} of ${covered} cap samples lie outside the outline (the wedge)`)
  // small self-intersection loops are cut from flattened rings
  const loop = flatten.removeSmallLoops([0, 0, 1, 0, 1, 1, 0.5, 1, 0.52, 1.02, 0.51, 0.98, 0.49, 1.0, 0, 1])
  assert.ok(loop.length < 16)
})

// ------------------------------------------------------------------------------------------------ round 5 (review)
test('round 5 review: prism glass keeps the body colour once (Glass BSDF), Principled glass presets sqrt(body)', () => {
  // The worker's prism is built from Glass BSDFs whose colour (glass_colors' sqrt(body)) tints BOTH interfaces, while
  // clear / frosted / tinted glass are Principled (sqrt(body) once): only the latter take the GLASS_TRANSMIT calibration.
  const mat = read('blender_worker/materials.py')
  const prism = /def b_dispersive_crystal[\s\S]*?\n(?=def )/.exec(mat)[0]
  assert.match(prism, /base, base_m = glass_colors\(c, col, tint\)/)
  assert.match(prism, /g\.node\("ShaderNodeBsdfGlass"/)
  assert.doesNotMatch(prism, /_glass_common\(/)
  for (const id of ['clear_glass', 'tinted_glass']) assert.match(/def b_\w+[\s\S]*?\n(?=def )/g.exec(mat.slice(mat.indexOf(`def b_${id}(`)))[0], /_glass_common\(/)
  assert.match(/def b_frosted_glass[\s\S]*?\n(?=def )/.exec(mat)[0], /ShaderNodeBsdfPrincipled/)
  const sqrtLine = /bisTransTint = 0\.72\d* \* sqrt\( max\( bisPm, vec3\( 0\.0 \) \) \) \/ max\( bisPm, vec3\( 1e-4 \) \);/
  for (const id of ['clear_glass', 'frosted_glass', 'tinted_glass', 'dispersive_crystal']) {
    const s = m3d.describeMaterial({ preset: id, params: {} }, presets)
    assert.equal(s.transmitBody, id === 'dispersive_crystal', id)
    const m = new m3d.IconMaterial()
    m3d.applyIconMaterial(m, s, ctx({ displayPaint: 'brand' }))
    assert.equal('BIS_TRANSMIT_BODY' in m.defines, id === 'dispersive_crystal', id)
    const { shader, warnings } = compile(m)
    assert.deepEqual(warnings, [])
    assert.match(shader.fragmentShader, sqrtLine) // present in the source, compiled out by the guard for prism
    assert.match(shader.fragmentShader, /#if defined\( BIS_PAINT_PERCEPTUAL \) && !defined\( BIS_TRANSMIT_BODY \)/)
  }
  assert.equal(m3d.describeMaterial({ preset: 'liquid_glass', params: {} }, presets).transmitBody, false)
})

test('round 5 review: live neon bloom follows the worker Glare strength curve (threshold 0.3, × NEON_BLOOM_LIVE)', () => {
  // Calibrated on 8 neon-look icons against 256 px Cycles previews; the round-3 values (0.15 / 0.4 + 1.2 × bloom) laid a
  // grey veil over the dark plate that 'brand' (no PBR Neutral toe) shows: a worker Glare change fails here.
  const r = read('blender_worker/render.py')
  assert.equal(pyConst(r, 'BLOOM_THRESHOLD'), 0.35)
  assert.match(r, /gl\.inputs\["Strength"\]\.default_value = 0\.3 \+ 0\.9 \* bloom/)
  assert.match(r, /gl\.inputs\["Size"\]\.default_value = 0\.2 \+ 0\.3 \* bloom/)
  const fx = read('web/src/viewport/scene/Effects.tsx')
  assert.match(fx, /^const NEON_BLOOM_THRESHOLD = 0\.3$/m)
  assert.match(fx, /^const NEON_BLOOM_LIVE = 0\.25 \/ 0\.3$/m)
  assert.match(fx, /luminanceThreshold: NEON_BLOOM_THRESHOLD,/)
  assert.match(fx, /effect\.intensity = NEON_BLOOM_LIVE \* \(0\.3 \+ 0\.9 \* strength\)/)
  assert.doesNotMatch(fx, /0\.4 \+ 1\.2 \* strength/)
})

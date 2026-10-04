// Live-view materials (PLAN §11): ONE Principled BSDF per shape, mapped input by input onto MeshPhysicalMaterial
// (lib/materials3d.ts), and the appearance renditions that only change Principled inputs (lib/appearance.ts).
// The worker (blender_worker/materials.py, presets.py, appearance.py) and shared/presets.json are the reference.
// Run (from web/): node --test src/viewport/tests/
import { read, presets, pyConst, pyDict } from './helpers.mjs'
import { existsSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const THREE = await import('three')
const m3d = await import('../../lib/materials3d.ts')
const ap = await import('../../lib/appearance.ts')

const near = (a, b, eps = 1e-6, msg = '') => assert.ok(Math.abs(a - b) <= eps, `${msg} ${a} ≉ ${b}`)
const paint = (r = 0.2, g = 0.4, b = 0.8) => ({ map: null, color: new THREE.Color(r, g, b) })

// ------------------------------------------------------------------------------------------------ schema / resolution
test('the viewport reads the whole Principled schema of every preset (28 params, no fake params)', () => {
  const keys = Object.keys(m3d.DEFAULT_PRINCIPLED).sort()
  assert.equal(keys.length, 28)
  for (const [id, mat] of Object.entries(presets.materials)) {
    assert.deepEqual(Object.keys(mat.params).sort(), keys, id)
    const p = m3d.materialParams(id, {}, presets)
    for (const [k, s] of Object.entries(mat.params)) assert.equal(p[k], s.default, `${id}.${k}`)
  }
  // the built-in fallback (harness without a backend) is Liquid Glass
  const lg = presets.materials.liquid_glass.params
  for (const k of keys) assert.equal(m3d.DEFAULT_PRINCIPLED[k], lg[k].default, k)
  assert.deepEqual(m3d.materialParams('liquid_glass', {}, null), m3d.DEFAULT_PRINCIPLED)
})

test('legacy names map like the worker (presets.LEGACY_PARAMS); values clamp to the schema; unknown params drop', () => {
  assert.deepEqual(m3d.LEGACY_PARAMS, pyDict(read('blender_worker/presets.py'), 'LEGACY_PARAMS'))
  const p = m3d.materialParams('liquid_glass', { frost: 0.3, coat: 0.9, ior: 0.2, paintMode: 'nope', glow: 1, __intent: 'mono' }, presets)
  assert.equal(p.roughness, 0.3)
  assert.equal(p.coatWeight, 0.9)
  assert.equal(p.ior, 1) // schema min
  assert.equal(p.paintMode, 'base')
  assert.ok(!('glow' in p) && !('__intent' in p) && !('frost' in p))
  // the new name wins over its legacy name
  assert.equal(m3d.materialParams('liquid_glass', { frost: 0.3, roughness: 5 }, presets).roughness, 1)
})

test('per-shape materials merge like presets.resolve_material (Layer.elementMaterials)', () => {
  const layerMat = { preset: 'liquid_glass', params: { roughness: 0.3, tint: 0.5 } }
  // same preset: layer params + the shape's own
  let r = m3d.resolveMaterial(layerMat, { preset: 'liquid_glass', params: { ior: 2 } }, presets)
  assert.equal(r.preset, 'liquid_glass')
  assert.deepEqual([r.params.roughness, r.params.tint, r.params.ior], [0.3, 0.5, 2])
  // another preset starts from that preset's defaults (the layer's params were tuned for its own preset)
  r = m3d.resolveMaterial(layerMat, { preset: 'chrome', params: { roughness: 0.15 } }, presets)
  assert.equal(r.preset, 'chrome')
  assert.equal(r.params.metallic, 1)
  assert.equal(r.params.roughness, 0.15)
  assert.equal(r.params.tint, presets.materials.chrome.params.tint.default)
  // unknown presets → liquid_glass
  assert.equal(m3d.resolveMaterial({ preset: 'nope', params: {} }, null, presets).preset, 'liquid_glass')
  // shapes of a layer: elementMaterials apply only with Effects on (glass); off = the unlit 'flat' preset
  const layer = { glass: true, material: layerMat, elementMaterials: { e2: { preset: 'chrome', params: {} } } }
  assert.equal(m3d.shapeMaterial(layer, 'e1', presets).preset, 'liquid_glass')
  assert.equal(m3d.shapeMaterial(layer, 'e2', presets).preset, 'chrome')
  assert.equal(m3d.shapeMaterial({ ...layer, glass: false }, 'e2', presets).preset, 'flat')
  // unextruded <image> cards: 'flat' unless the shape has its own material
  assert.equal(m3d.shapeMaterial(layer, 'img', presets, m3d.FLAT_MATERIAL).preset, 'flat')
  assert.equal(m3d.shapeMaterial(layer, 'e2', presets, m3d.FLAT_MATERIAL).preset, 'chrome')
  assert.ok(m3d.hasOwnMaterials(layer, ['e1', 'e2']))
  assert.ok(!m3d.hasOwnMaterials(layer, ['e1']))
  assert.ok(!m3d.hasOwnMaterials({ ...layer, glass: false }, ['e2']))
})

// ------------------------------------------------------------------------------------------------ the mapping
test('every preset maps input by input onto ONE MeshPhysicalMaterial (materials.py PRINCIPLED)', () => {
  for (const id of Object.keys(presets.materials)) {
    const p = m3d.materialParams(id, {}, presets)
    const m = new m3d.PrincipledMaterial()
    m3d.applyPrincipled(m, p, { paint: paint(), opacity: 1, thickness: 0.09 })
    assert.ok(m instanceof THREE.MeshPhysicalMaterial)
    near(m.metalness, p.metallic, 1e-9, id)
    near(m.roughness, p.roughness, 1e-9, id)
    near(m.ior, Math.min(2.333, p.ior), 1e-9, id) // three.js caps IOR at 2.333 (Diamond 2.4)
    near(m.transmission, p.transmission, 1e-9, id)
    near(m.thickness, p.transmission > 0 ? 0.09 : 0, 1e-9, id)
    near(m.clearcoat, p.coatWeight, 1e-9, id)
    near(m.clearcoatRoughness, p.coatRoughness, 1e-9, id)
    near(m.sheen, p.sheenWeight, 1e-9, id)
    near(m.sheenRoughness, p.sheenRoughness, 1e-9, id)
    near(m.specularIntensity, 2 * p.specularIorLevel, 1e-9, id) // 0.5 = no adjustment
    near(m.emissiveIntensity, p.emissionStrength, 1e-9, id)
    assert.equal(m.iridescence, p.thinFilmThickness > 0 ? 1 : 0, id)
    near(m.iridescenceIOR, Math.min(2.333, p.thinFilmIor), 1e-9, id)
    const v = p.filmVariation
    assert.deepEqual(m.iridescenceThicknessRange, [p.thinFilmThickness * (1 - v), p.thinFilmThickness * (1 + v)], id)
    assert.equal(!!m.iridescenceThicknessMap, p.thinFilmThickness > 0 && v > 0, id)
    near(m.anisotropy, p.anisotropic, 1e-9, id)
    assert.equal(!!m.anisotropyMap, p.anisotropic > 0, id) // radial tangent
    assert.equal(!!m.normalMap, p.grain > 0, id) // Noise → Bump
    near(m.dispersion, 0, 0, id) // no dispersion in Blender 5.0's Principled
    // paint pre-processing: Base = Mix(white → art, tint) (emission-painted: from black), Emission colour = art
    near(m.bis.bisTint.value, p.tint, 1e-9, id)
    assert.equal(m.bis.bisPaintFrom.value.getHex(), p.paintMode === 'emission' ? 0x000000 : 0xffffff, id)
    assert.equal(m.bis.bisEmitArt.value, p.paintMode === 'base' ? 0 : 1, id)
    assert.equal(m.color.getHex(), 0xffffff, id)
    assert.equal(m.emissive.getHex(), 0xffffff, id)
  }
})

test('specular / sheen tints mix white → art colour; alpha × piece opacity; translucent bodies route to the opaque pass', () => {
  const p = m3d.materialParams('satin', { specularTint: 1, sheenWeight: 1, sheenTint: 0.5, alpha: 0.5 }, presets)
  const m = new m3d.PrincipledMaterial()
  const art = new THREE.Color(0.2, 0.4, 0.8)
  m3d.applyPrincipled(m, p, { paint: { map: null, color: art }, opacity: 0.8, thickness: 0.1 })
  ;['r', 'g', 'b'].forEach((k) => near(m.specularColor[k], art[k], 1e-9, `specular ${k}`))
  near(m.sheenColor.g, 0.5 + 0.5 * 0.4, 1e-6)
  near(m.opacity, 0.4, 1e-9)
  // translucent + not on top of glass: drawn in the opaque pass with explicit blending
  assert.ok(m.routed && !m.transparent && m.blending === THREE.CustomBlending)
  m3d.applyPrincipled(m, p, { paint: { map: null, color: art }, opacity: 0.8, thickness: 0.1, route: false })
  assert.ok(!m.routed && m.transparent && m.blending === THREE.NormalBlending)
  // opaque again: normal blending
  m3d.applyPrincipled(m, m3d.materialParams('satin', {}, presets), { paint: paint(), opacity: 1, thickness: 0.1 })
  assert.ok(!m.transparent && m.blending === THREE.NormalBlending)
  // a raster image's alpha multiplies Alpha
  m3d.applyPrincipled(m, m3d.materialParams('flat', {}, presets), {
    paint: { map: new THREE.Texture(), color: art, alpha: true, uv: [0.5, 0, 0.5, 0, 0.5, 0.5] },
    opacity: 1,
    thickness: 0.01,
  })
  assert.equal(m.bis.bisArtAlpha.value, 1)
  assert.ok(m.map && m.map.matrixAutoUpdate === false) // placed clone: art square → image UV
  near(m.map.matrix.elements[0], 1, 1e-9) // 2 × a
})

test('glass seen through glass is drawn opaque × what lies behind it (no transmission through transmission)', () => {
  const p = m3d.materialParams('liquid_glass', {}, presets)
  const m = new m3d.PrincipledMaterial()
  const behind = new THREE.Color(0.9, 0.5, 0.2)
  m3d.applyPrincipled(m, p, { paint: paint(), opacity: 1, thickness: 0.1, covered: behind })
  assert.equal(m.transmission, 0)
  assert.ok(m.covered && m.bis.bisBehind.value.equals(behind) && m.bis.bisCovered.value === 1)
  m3d.applyPrincipled(m, p, { paint: paint(), opacity: 1, thickness: 0.1, contact: 0.7 })
  assert.equal(m.transmission, 1)
  assert.equal(m.bis.bisBehind.value.getHex(), 0xffffff)
  near(m.bis.bisContact.value, 0.7, 1e-12)
  // opaque presets ignore it
  m3d.applyPrincipled(m, m3d.materialParams('satin', {}, presets), { paint: paint(), opacity: 1, thickness: 0.1, covered: behind })
  assert.ok(!m.covered && m.bis.bisBehind.value.getHex() === 0xffffff)
  // only the TRANSMITTED share shows what lies behind (review r7: the dark renditions' DARK_GLYPH glass — transmission
  // 0.5 — covered by more glass was drawn as base × the dark plate: Earth / Find Device / Weatherbug went black)
  const dark = m3d.materialParams('liquid_glass', { transmission: 0.5, subsurfaceWeight: 1 }, presets)
  m3d.applyPrincipled(m, dark, { paint: paint(), opacity: 1, thickness: 0.1, covered: behind })
  assert.equal(m.transmission, 0)
  assert.ok(m.covered)
  near(m.bis.bisCovered.value, 0.5, 1e-12)
})

test('the shader patch only adds the paint pre-processing to three.js MeshPhysicalMaterial', () => {
  const m = new m3d.PrincipledMaterial()
  const shader = { uniforms: {}, vertexShader: THREE.ShaderLib.physical.vertexShader, fragmentShader: THREE.ShaderLib.physical.fragmentShader }
  m.onBeforeCompile(shader, null)
  const fs = shader.fragmentShader
  assert.ok(!fs.includes('#include <map_fragment>'))
  assert.match(fs, /mix\( bisPaintFrom, bisArt\.rgb, bisTint \)/) // Base Color = Mix(white → art, tint)
  assert.match(fs, /mix\( bisBase, bisBase \* pow\( max\( bisBase, vec3\( 1e-4 \) \), vec3\( bisContact \) \) \* bisBehind, bisCovered \)/) // covered glass
  assert.match(fs, /totalEmissiveRadiance \*= mix\( vec3\( 1\.0 \), bisArt\.rgb, bisEmitArt \)/) // Emission Color = art
  // transmission takes Base Color once (Cycles: √ per interface), once more when the body lies on what it shows
  assert.match(fs, /material\.diffuseContribution \*= pow\( max\( material\.diffuseContribution, vec3\( 1e-4 \) \), vec3\( bisContact \) \);\s*#endif\s*#include <transmission_fragment>/)
  for (const u of ['bisPaintFrom', 'bisTint', 'bisArtColor', 'bisArtAlpha', 'bisEmitArt', 'bisMono', 'bisMonoTint', 'bisBehind', 'bisCovered', 'bisContact'])
    assert.ok(shader.uniforms[u], u)
  assert.equal(shader.vertexShader, THREE.ShaderLib.physical.vertexShader)
})

test('bloom follows the worker scene info (compositor Glare from emission strength only)', () => {
  const mp = (id) => m3d.materialParams(id, {}, presets)
  assert.equal(m3d.bloomAmount([mp('liquid_glass'), mp('chrome')]), 0)
  assert.equal(m3d.bloomAmount([mp('flat')]), 0) // emission 1 = the flat-art level
  near(m3d.bloomAmount([mp('neon')]), (presets.materials.neon.params.emissionStrength.default - 1) / 4, 1e-9)
  assert.equal(m3d.bloomAmount([m3d.materialParams('neon', { emissionStrength: 30 }, presets)]), 1)
  // 'base' paint mode emits white, never blooms (materials.max_emission)
  assert.equal(m3d.bloomAmount([m3d.materialParams('satin', { emissionStrength: 9 }, presets)]), 0)
})

test('the fake-mirroring modules are gone', () => {
  for (const f of ['web/src/lib/overlay3d.ts', 'web/src/viewport/geometry/pillGeometry.ts', 'web/src/viewport/geometry/fillet.ts', 'web/src/viewport/textures/softAlpha.ts'])
    assert.ok(!existsSync(new URL(`../../../../${f}`, import.meta.url)), f)
})

// ------------------------------------------------------------------------------------------------ appearances
test('rendition constants mirror blender_worker/appearance.py', () => {
  const src = read('blender_worker/appearance.py')
  assert.deepEqual({ ...ap.CLEAR_GLYPH }, pyDict(src, 'CLEAR_GLYPH'))
  assert.deepEqual({ ...ap.CLEAR_PLATE }, pyDict(src, 'CLEAR_PLATE'))
  assert.deepEqual({ ...ap.TINTED_GLYPH }, pyDict(src, 'TINTED_GLYPH'))
  assert.deepEqual({ ...ap.TINTED_PLATE }, pyDict(src, 'TINTED_PLATE'))
  assert.deepEqual({ ...ap.DARK_GLYPH }, pyDict(src, 'DARK_GLYPH'))
  assert.equal(ap.MONO_FLOOR_DARK, pyConst(src, 'MONO_FLOOR_DARK'))
  assert.equal('TINTED_DARK_GLOW' in ap, false, 'round 7: tinted-dark glyphs are lit, not emissive')
  assert.equal(ap.MONO_FLOOR, pyConst(src, 'MONO_FLOOR'))
  assert.equal(ap.MONO_MIN_RANGE, pyConst(src, 'MONO_MIN_RANGE'))
  for (const [k, w] of Object.entries(ap.WALLPAPERS)) {
    assert.ok(src.includes(`"top": "${w.top}", "bottom": "${w.bottom}"`), k)
    for (const b of w.blobs) assert.ok(src.includes(`(${b.x}, ${b.y}, ${b.r}, "${b.color}")`), `${k} blob ${b.color}`)
  }
})

function project(over = {}) {
  const layer = (id, visible, extra = {}) => ({
    id,
    name: id,
    elementIds: [`${id}e`],
    visible,
    locked: false,
    mode: 'individual',
    fill: { type: 'auto' },
    opacity: 1,
    blendMode: 'normal',
    glass: true,
    transform: { x: 0, y: 0, scale: 1 },
    depth: { z: 0, thickness: 0.1, bevel: 0.045, bevelSegments: 6, inflate: 0 },
    material: { preset: 'candy', params: { roughness: 0.4 } },
    elementMaterials: { [`${id}e`]: { preset: 'chrome', params: {} } },
    shadow: { kind: 'physical', opacity: 0.5 },
    ...extra,
  })
  return {
    version: 1,
    id: 'p',
    name: 'p',
    createdAt: '',
    updatedAt: '',
    source: { filename: 'x.svg', viewBox: [0, 0, 1, 1], warnings: [], plateDetected: true },
    strategy: 'smart',
    elements: [],
    layers: [layer('A', true), layer('B', false, { fill: { type: 'none' } })],
    canvas: {
      platform: 'ios',
      shape: 'squircle',
      cornerRadius: 0.225,
      plate: { visible: true, fill: { type: 'solid', color: '#ff8800', opacity: 1 }, material: { preset: 'glossy_plastic', params: {} }, thickness: 0.16, bevel: 0.04 },
      art: { scale: 1, x: 0, y: 0 },
    },
    lighting: { preset: 'studio', angle: -45, elevation: 45, intensity: 1, rim: 1, fill: 1, environment: 1, shadowSoftness: 0.5 },
    camera: { view: 'front', tiltX: 0, tiltY: 0, fov: 30, zoom: 1, iso: 0, explode: 1 },
    appearance: 'light',
    appearances: { dark: { plateFill: { type: 'system-dark' }, layers: {} }, mono: { plateFill: null, layers: {} }, tint: { color: '#3b82f6', strength: 0.8 } },
    render: { quality: 'preview', colorMode: 'brand', backdrop: 'transparent', backdropColor: '#1c1c22', autoPreview: true },
    ...over,
  }
}

test('renditions only change Principled inputs (appearance.resolve), per-shape overrides dropped', () => {
  const p = project()
  assert.equal(ap.resolveAppearance(p, 'light'), p)
  const dark = ap.resolveAppearance(p, 'dark')
  assert.deepEqual(dark.canvas.plate.fill, { type: 'system-dark' })
  near(dark.lighting.intensity, 0.85, 1e-12)
  near(dark.lighting.environment, 0.6, 1e-12)
  assert.equal(dark.layers[0].material.preset, 'candy')
  for (const id of ['clear-light', 'clear-dark']) {
    const c = ap.resolveAppearance(p, id)
    assert.deepEqual(c.layers[0].material, { preset: 'liquid_glass', params: { ...ap.CLEAR_GLYPH } })
    assert.deepEqual(c.layers[0].elementMaterials, {})
    assert.equal(c.layers[1].material.preset, 'candy') // hidden layers keep theirs
    assert.deepEqual(c.canvas.plate.material, { preset: 'frosted_glass', params: { ...ap.CLEAR_PLATE } })
    assert.deepEqual(c.canvas.plate.fill, { type: 'solid', color: '#ffffff', opacity: 1 })
  }
  const tl = ap.resolveAppearance(p, 'tinted-light')
  assert.deepEqual(tl.layers[0].material.params, { ...ap.TINTED_GLYPH })
  assert.equal(tl.canvas.plate.fill.color, ap.mixHex('#ffffff', '#3b82f6', 0.18 + 0.2 * 0.8))
  const td = ap.resolveAppearance(p, 'tinted-dark', presets)
  // round 7: lit glass (DARK_GLYPH), no emission — TINTED_GLYPH capped to half transmission + subsurface
  assert.deepEqual(td.layers[0].material, {
    preset: 'liquid_glass',
    params: { ...ap.TINTED_GLYPH, transmission: 0.5, subsurfaceWeight: 1, tint: 1 },
  })
  assert.equal(td.layers[0].material.params.emissionStrength, 0)
  assert.equal(ap.appearanceMono(p, 'tinted-dark', null).floor, ap.MONO_FLOOR_DARK)
  assert.deepEqual(td.canvas.plate.fill, { type: 'system-dark' })
  assert.equal(td.canvas.plate.material.preset, 'glossy_plastic') // a solid plate preset is kept
  // watchOS ignores appearances
  const w = project({ canvas: { ...p.canvas, platform: 'watchos' } })
  assert.equal(ap.resolveAppearance(w, 'clear-dark'), w)
  assert.equal(ap.appearanceMono(w, 'tinted-dark', null), null)
})

test('dark renditions keep glyphs readable: transmissive glass → DARK_GLYPH lit glass (worker _dark_glyphs)', () => {
  const glass = (id, extra = {}) => ({ ...project().layers[0], id, elementIds: [`${id}e`], material: { preset: 'liquid_glass', params: {} }, elementMaterials: {}, ...extra })
  const p = project({
    layers: [
      glass('A'), // Liquid Glass: transmission 1 → capped
      glass('B', { material: { preset: 'candy', params: { roughness: 0.4 } } }), // candy: not transmissive enough → unchanged
      glass('C', { elementMaterials: { Ce: { preset: 'clear_glass', params: { roughness: 0.1 } } } }), // per-shape glass too
      glass('D'), // the user set D's dark material → kept
      glass('E', { glass: false }), // glass effects off → flat, untouched
      glass('F', { visible: false }),
    ],
  })
  p.appearances = { ...p.appearances, dark: { plateFill: { type: 'system-dark' }, layers: { D: { material: { preset: 'chrome', params: {} } } } } }
  const dark = ap.resolveAppearance(p, 'dark', presets)
  const byId = Object.fromEntries(dark.layers.map((l) => [l.id, l]))
  const full = (l, eid = null) => m3d.shapeMaterial(l, eid, presets).params
  const a = full(byId.A)
  assert.equal(a.transmission, ap.DARK_GLYPH.transmission)
  assert.equal(a.subsurfaceWeight, ap.DARK_GLYPH.subsurfaceWeight)
  assert.ok(a.tint >= ap.DARK_GLYPH.tintMin, `tint ${a.tint}`)
  assert.equal(a.emissionStrength, 0, 'lit, not self-lit')
  assert.deepEqual(byId.B.material, { preset: 'candy', params: { roughness: 0.4 } })
  assert.ok(full(byId.B).transmission <= ap.DARK_GLYPH.transmission)
  const ce = full(byId.C, 'Ce')
  assert.equal(byId.C.elementMaterials.Ce.preset, 'clear_glass')
  assert.equal(ce.transmission, 0.5)
  assert.equal(ce.roughness, 0.1, 'the shape keeps its own inputs')
  assert.deepEqual(byId.D.material, { preset: 'chrome', params: {} })
  assert.deepEqual(byId.E.material, { preset: 'liquid_glass', params: {} })
  assert.deepEqual(byId.F.material, { preset: 'liquid_glass', params: {} })
  // the light rendition is untouched; without presets the built-in Liquid Glass defaults decide
  assert.equal(ap.resolveAppearance(p, 'light', presets), p)
  assert.equal(full(ap.resolveAppearance(p, 'dark').layers[0]).transmission, 0.5)
  // mirrors the worker's rules (DARK_GLYPH applied through _dark_params in both dark renditions)
  const src = read('blender_worker/appearance.py')
  assert.match(src, /_dark_glyphs\(proj, \{lid for lid, lo in/)
  assert.match(src, /_glass_layers\(proj, TINTED_GLYPH\)\s+_dark_glyphs\(proj, set\(\)\)/)
})

test("tinted renditions: the worker's mono transform (luminance range over the visible paint × tint)", () => {
  const p = project()
  const geometry = {
    A: { regions: [{ paint: { type: 'solid', color: '#ffffff' } }, { paint: { type: 'solid', color: '#808080' } }] },
  }
  assert.equal(ap.appearanceMono(p, 'clear-light', geometry), null)
  const m = ap.appearanceMono(p, 'tinted-light', geometry)
  const lin = (v) => ap.srgbToLinear(v / 255)
  const lo = lin(128)
  near(m.hi, 1, 1e-9)
  near(m.lo, Math.min(lo, 1 - ap.MONO_MIN_RANGE), 1e-9)
  assert.equal(m.floor, ap.MONO_FLOOR)
  const t = [0x3b, 0x82, 0xf6].map(lin)
  m.tint.forEach((v, i) => near(v, 1 + (t[i] - 1) * 0.8, 1e-9, `tint ${i}`))
  // applied by the material: art luminance stretched to floor..1 × tint
  const mat = new m3d.PrincipledMaterial()
  m3d.applyPrincipled(mat, m3d.materialParams('liquid_glass', ap.TINTED_GLYPH, presets), { paint: paint(), opacity: 1, thickness: 0.1, mono: m })
  assert.equal(mat.bis.bisMono.value.w, 1)
  near(mat.bis.bisMono.value.x, m.lo, 1e-6)
})

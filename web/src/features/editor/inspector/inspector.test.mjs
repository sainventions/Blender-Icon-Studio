// Round-6 (PLAN §11) inspector model tests: one Principled BSDF per shape edited in Blender's panel groups, per-shape
// materials merged like blender_worker presets.resolve_material, Roundness / re-stack depth helpers, and the retired
// explode / art-directed-shadow UI staying gone.
// Run (from web/): node --test src/features/editor/inspector/*.test.mjs   (Node ≥ 23.6: built-in TS type stripping)
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  fallbackParam,
  groupActive,
  groupGate,
  optionLabel,
  overriddenElements,
  paramOverridden,
  principledGroups,
  resolvedParam,
  shapeMaterial,
  updateElementMaterials,
} from './principled.ts'
import { bevelLimit, restack, roundnessOf, STACK_MIN_STEP, withRoundness, withThickness } from './depth.ts'

const WEB = new URL('../../../../', import.meta.url)
const read = (p) => readFileSync(new URL(p, WEB), 'utf8')
const presets = JSON.parse(read('../shared/presets.json'))

const active = (id, spec = { preset: id, params: {} }) => {
  const preset = presets.materials[id]
  const value = (k) => resolvedParam(preset, spec, k)
  return principledGroups(presets, preset)
    .filter((g) => groupActive(g, value))
    .map((g) => g.name)
}

function layer(over = {}) {
  return {
    id: 'L1',
    name: 'L1',
    elementIds: ['e1', 'e2'],
    visible: true,
    locked: false,
    mode: 'individual',
    fill: { type: 'auto' },
    opacity: 1,
    blendMode: 'normal',
    glass: true,
    transform: { x: 0, y: 0, scale: 1 },
    depth: { z: 0, thickness: 0.1, bevel: 0.045, bevelSegments: 6, inflate: 0 },
    material: { preset: 'liquid_glass', params: {} },
    elementMaterials: {},
    shadow: { kind: 'physical', opacity: 0.5 },
    ...over,
  }
}

// ------------------------------------------------------------------------------------------ Principled panel
test('every preset shows the same 28 Principled inputs in Blender panel order', () => {
  const order = presets.principledSchema.groups
  assert.deepEqual(order, ['Paint', 'Base', 'Subsurface', 'Specular', 'Transmission', 'Coat', 'Sheen', 'Emission', 'Thin Film'])
  for (const [id, preset] of Object.entries(presets.materials)) {
    const groups = principledGroups(presets, preset)
    assert.deepEqual(groups.map((g) => g.name), order, id)
    const keys = groups.flatMap((g) => g.params.map(([k]) => k))
    assert.equal(keys.length, 28, id)
    assert.deepEqual(new Set(keys), new Set(Object.keys(preset.params)), id)
  }
})

test('only groups that do something start expanded (gated lobes by their weight)', () => {
  assert.deepEqual(active('liquid_glass'), ['Paint', 'Base', 'Transmission', 'Coat'])
  assert.deepEqual(active('chrome'), ['Paint', 'Base'])
  assert.deepEqual(active('candy'), ['Paint', 'Base', 'Subsurface', 'Coat'])
  assert.deepEqual(active('neon'), ['Paint', 'Base', 'Emission'])
  assert.deepEqual(active('dispersive_crystal'), ['Paint', 'Base', 'Transmission', 'Thin Film'])
  assert.ok(active('matte_clay').includes('Specular'), 'IOR Level 0.3 differs from a fresh Principled node')
  assert.ok(active('matte_clay').includes('Sheen'))
  // a user edit switches a lobe on
  assert.ok(active('chrome', { preset: 'chrome', params: { coatWeight: 0.4 } }).includes('Coat'))
  const coat = principledGroups(presets, presets.materials.chrome).find((g) => g.name === 'Coat')
  assert.equal(groupGate(coat), 'coatWeight')
})

test('values: own param, else the inherited material (same preset only), else the preset default', () => {
  const lg = presets.materials.liquid_glass
  const layerMat = { preset: 'liquid_glass', params: { roughness: 0.27 } }
  const own = { preset: 'liquid_glass', params: { ior: 2 } }
  assert.equal(resolvedParam(lg, own, 'ior', layerMat), 2)
  assert.equal(resolvedParam(lg, own, 'roughness', layerMat), 0.27)
  assert.equal(resolvedParam(lg, own, 'coatWeight', layerMat), 0.5)
  assert.equal(fallbackParam(lg, own, 'ior', layerMat), 1.5)
  assert.equal(paramOverridden(lg, own, 'ior', layerMat), true)
  assert.equal(paramOverridden(lg, { preset: 'liquid_glass', params: { roughness: 0.27 } }, 'roughness', layerMat), false)
  // another preset ignores the layer's tuning
  const chrome = presets.materials.chrome
  assert.equal(resolvedParam(chrome, { preset: 'chrome', params: {} }, 'roughness', layerMat), chrome.params.roughness.default)
  assert.equal(optionLabel('paintMode', 'base+emission'), 'Both')
})

// ------------------------------------------------------------------------------------------ per-shape materials
test('a shape material merges like the worker: same preset = layer params + own; other preset starts fresh', () => {
  const layerMat = { preset: 'liquid_glass', params: { roughness: 0.27, ior: 1.6 } }
  assert.equal(shapeMaterial(layerMat, undefined), layerMat)
  assert.deepEqual(shapeMaterial(layerMat, { preset: 'liquid_glass', params: { ior: 2 } }), {
    preset: 'liquid_glass',
    params: { roughness: 0.27, ior: 2 },
  })
  assert.deepEqual(shapeMaterial(layerMat, { preset: 'chrome', params: { roughness: 0.1 } }), { preset: 'chrome', params: { roughness: 0.1 } })
})

test('updateElementMaterials adds / removes overrides of the layer’s own shapes only', () => {
  const l = layer()
  const a = updateElementMaterials(l, ['e2', 'e9'], () => ({ preset: 'chrome', params: {} }))
  assert.deepEqual(a.elementMaterials, { e2: { preset: 'chrome', params: {} } })
  assert.deepEqual(overriddenElements(a), ['e2'])
  assert.equal(updateElementMaterials(a, ['e2'], (cur) => cur), a, 'no-op keeps the object')
  const b = updateElementMaterials(a, ['e1', 'e2'], () => undefined)
  assert.deepEqual(b.elementMaterials, {})
  assert.deepEqual(l.elementMaterials, {}, 'input untouched')
})

// ------------------------------------------------------------------------------------------ depth
test('Roundness = bevel / (thickness / 2) — the renderers’ own clamp; 1 = full pill edge', () => {
  const l = layer()
  assert.equal(bevelLimit(l), 0.05)
  assert.ok(Math.abs(roundnessOf(l) - 0.9) < 1e-9)
  assert.equal(roundnessOf(layer({ depth: { ...layer().depth, bevel: 0.2 } })), 1, 'clamped display')
  assert.equal(withRoundness(l, 1).depth.bevel, 0.05)
  assert.equal(withRoundness(l, 0.5).depth.bevel, 0.025)
  // height-field bodies taper thin parts themselves: no thin-feature (safeRadius) cap on the slider any more
  const depth = read('src/features/editor/inspector/depth.ts')
  assert.ok(!/safeRadius/.test(depth.replace(/^\s*\/\/.*$/gm, '')), 'Roundness does not read the curve-bevel safe radius')
})

test('changing thickness keeps the roundness', () => {
  const l = withRoundness(layer(), 1)
  const t = withThickness(l, 0.3)
  assert.equal(t.depth.thickness, 0.3)
  assert.equal(t.depth.bevel, 0.15, 'still a full pill')
  const half = withThickness(withRoundness(layer(), 0.5), 0.2)
  assert.equal(half.depth.bevel, 0.05, '50 % stays 50 %')
  assert.equal(withThickness(l, 0.1), l)
})

test('re-stack leaves room for each body (its measured height) and keeps locked layers', () => {
  const a = layer({ id: 'L1', depth: { z: 0.5, thickness: 0.2, bevel: 0.05, bevelSegments: 6, inflate: 0.5 } })
  const b = layer({ id: 'L2', depth: { z: 0.0, thickness: 0.1, bevel: 0.05, bevelSegments: 6, inflate: 0 } })
  // the editor measures inflated domes like blender_worker scene._body_height (viewport layerBodyHeight)
  const height = (l) => (l.id === 'L1' ? 0.3 : l.depth.thickness)
  const [a2, b2] = restack([a, b], height)
  assert.equal(a2.depth.z, 0)
  assert.ok(Math.abs(b2.depth.z - 0.33) < 1e-9, `${b2.depth.z}`)
  const [, c2] = restack([layer(), layer({ id: 'L2' })])
  assert.equal(c2.depth.z, STACK_MIN_STEP, 'never tighter than the import default')
  const same = [layer({ depth: { ...layer().depth, z: 0 } })]
  assert.equal(restack(same), same)
  const locked = layer({ id: 'L1', locked: true, depth: { ...layer().depth, z: 0.6 } })
  const [l2, n2] = restack([locked, layer({ id: 'L2' })])
  assert.equal(l2, locked)
  assert.ok(Math.abs(n2.depth.z - 0.73) < 1e-9)
  // the Depth section's Re-stack button measures bodies with the viewport's mirror of the worker
  const inspector = read('src/features/editor/inspector/LayerInspector.tsx')
  assert.match(inspector, /restack\(p\.layers, height\)/)
  assert.match(inspector, /layerBodyHeight\(l, lg, layerScale\(p\.canvas\.art\.scale, l\.transform\.scale\)\)/)
})

// ------------------------------------------------------------------------------------------ retired UI stays gone
test('no explode control, CAD view bound to camera.iso, shadows are physical / none', () => {
  const stage = read('src/features/editor/stage/Stage.tsx')
  assert.ok(!/explode/i.test(stage), 'Stage has no explode control')
  assert.match(stage, /iso=\{isoAnim \?\? project\.camera\.iso \?\? 0\}/)
  // the View control lives in the stage toolbar, hidden in the Matrix (head-on renditions) like the other view controls
  const toolbar = stage.slice(stage.indexOf('function StageToolbar'), stage.indexOf('function ViewAngleControl'))
  assert.ok(toolbar.indexOf('<ViewAngleControl />') > toolbar.indexOf("{mode !== 'matrix' && ("))
  assert.match(stage, /onChange=\{\(v\) => setIso\(v\)\}/)
  const shortcuts = read('src/features/editor/shortcuts.ts')
  assert.ok(!/explode/i.test(shortcuts))
  assert.match(shortcuts, /case 'i':\s*\n\s*if \(ui\.stageMode === 'matrix'\) return/)
  assert.ok(!/explode/i.test(read('src/store/ui.ts')))
  assert.ok(!/explode/i.test(read('src/features/editor/inspector/DocumentInspector.tsx')))
  const inspector = read('src/features/editor/inspector/LayerInspector.tsx')
  assert.ok(!/'neutral'|'chromatic'|Chromatic/.test(inspector), 'no art-directed shadow kinds')
  assert.ok(!/Glass effects/.test(inspector), 'no Icon Composer glass toggle')
  assert.match(read('src/lib/meta.ts'), /id: 'iso', label: 'Iso sweep'/)
  assert.doesNotMatch(read('src/lib/meta.ts'), /id: 'explode'/)
  assert.match(read('src/features/dialogs/AnimateDialog.tsx'), /ANIMATION_KINDS\.filter/)
  assert.ok(!/MaterialParams/.test(read('src/features/editor/inspector/MaterialGallery.tsx')))
})

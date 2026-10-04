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
import {
  bevelLimit,
  DEFAULT_STACK_RULES,
  keepStack,
  restack,
  roundnessOf,
  ruleHeight,
  stackAffected,
  stackGapOf,
  stackRules,
  withRoundness,
  withThickness,
} from './depth.ts'

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

test('the stacking rule is presets.json "geometry" + server bis.stacking: H = thickness + 2·inflate·maxRadius·S', () => {
  // the same numbers as the server / worker (shared/presets.json "geometry")
  assert.deepEqual(stackRules(presets), { stackLift: presets.geometry.stackLift, stackGap: presets.geometry.stackGap })
  assert.deepEqual(stackRules(null), DEFAULT_STACK_RULES)
  assert.deepEqual(stackRules({ geometry: { stackGap: -1, stackLift: Number.NaN } }), { stackLift: 0, stackGap: 0 })
  const stacking = read('../server/bis/stacking.py')
  assert.match(stacking, /DEFAULT_RULES: dict\[str, float\] = \{"stackLift": 0\.0, "stackGap": 0\.03\}/)
  assert.equal(DEFAULT_STACK_RULES.stackGap, 0.03)
  const l = layer({ depth: { z: 0, thickness: 0.16, bevel: 0.08, bevelSegments: 8, inflate: 0.25 } })
  // server stacking.body_height: t + 2·k·maxRadius·S (S = art.scale × layer.scale); inflate clamped to 0..1
  assert.ok(Math.abs(ruleHeight(l, 0.34141, 1.0729) - (0.16 + 2 * 0.25 * 0.34141 * 1.0729)) < 1e-12)
  assert.equal(ruleHeight(layer({ depth: { ...l.depth, inflate: 3 } }), 0.2, 1), 0.16 + 2 * 0.2)
  assert.equal(ruleHeight(l, undefined, 1), 0.16, 'no maxRadius: the thickness')
})

test('Re-stack: z0 = stackLift, z(i+1) = z(i) + H(i) + stackGap; locked layers keep their z', () => {
  const a = layer({ id: 'L1', depth: { z: 0.5, thickness: 0.2, bevel: 0.05, bevelSegments: 6, inflate: 0.5 } })
  const b = layer({ id: 'L2', depth: { z: 0.0, thickness: 0.1, bevel: 0.05, bevelSegments: 6, inflate: 0 } })
  const height = (l) => (l.id === 'L1' ? 0.3 : l.depth.thickness)
  const [a2, b2] = restack([a, b], height)
  assert.equal(a2.depth.z, 0)
  assert.ok(Math.abs(b2.depth.z - 0.33) < 1e-9, `${b2.depth.z}`)
  // the presets' gap and lift; no 0.13 floor any more (round 7: real heights, one clearance)
  const [c1, c2] = restack([layer(), layer({ id: 'L2' })], undefined, { stackLift: 0.01, stackGap: 0.02 })
  assert.equal(c1.depth.z, 0.01)
  assert.ok(Math.abs(c2.depth.z - (0.01 + 0.1 + 0.02)) < 1e-9, `${c2.depth.z}`)
  const same = [layer({ depth: { ...layer().depth, z: 0 } })]
  assert.equal(restack(same), same)
  const locked = layer({ id: 'L1', locked: true, depth: { ...layer().depth, z: 0.6 } })
  const [l2, n2] = restack([locked, layer({ id: 'L2' })])
  assert.equal(l2, locked)
  assert.ok(Math.abs(n2.depth.z - 0.73) < 1e-9)
  // the running z is not rounded (server stacking.stack_z): Photos' import stack (H = 0.25024641327) is a no-op —
  // accumulating the 5-decimal values gave 0.5605 / 0.84075 instead of the server's 0.56049 / 0.84074
  const photos = [0, 0.28025, 0.56049, 0.84074].map((z, i) => layer({ id: `P${i}`, depth: { ...layer().depth, z } }))
  assert.equal(restack(photos, () => 0.25024641327), photos)
  // the Depth section's Re-stack button uses the shared rule with the bundle's maxRadius (features/editor/stacking.ts)
  const inspector = read('src/features/editor/inspector/LayerInspector.tsx')
  assert.match(inspector, /restackProject\(p, geometry, presets\)/)
  const stacking = read('src/features/editor/stacking.ts')
  assert.match(stacking, /ruleHeight\(l, lg\?\.maxRadius, S\)/)
  assert.match(stacking, /layerScale\(p\.canvas\.art\.scale, l\.transform\.scale\)/)
})

test('a recognised real-height stack is kept across edits (server stack_gap); custom stacks are left alone', () => {
  const H = (l) => l.depth.thickness + 2 * l.depth.inflate * 0.2
  const mk = (zs, extra = {}) => zs.map((z, i) => layer({ id: `L${i + 1}`, depth: { z, thickness: 0.16, bevel: 0.08, bevelSegments: 8, inflate: 0.25 }, ...extra }))
  // 0.16 + 2·0.25·0.2 = 0.26 tall, 0.03 apart
  const stack = restack(mk([0, 0, 0]), H)
  assert.deepEqual(stack.map((l) => l.depth.z), [0, 0.29, 0.58])
  assert.equal(stackGapOf(stack, H), 0.03)
  assert.equal(stackGapOf(mk([0, 0.13, 0.26]), H), DEFAULT_STACK_RULES.stackGap, 'the legacy 0.13 stack converts')
  assert.equal(stackGapOf(mk([0, 0.4, 0.6]), H), null, 'hand-placed')
  assert.equal(stackGapOf(mk([0.05, 0.34, 0.63]), H), null, 'not starting at stackLift')
  assert.equal(stackGapOf(mk([0, 0.2, 0.5]), H), null, 'interpenetrating')
  const loose = restack(mk([0, 0, 0]), H, DEFAULT_STACK_RULES, 0.1) // a look's wider gap is recognised as such
  assert.equal(stackGapOf(loose, H), 0.1)
  // thicker middle layer: the layer above moves, the gap stays
  const after = stack.map((l, i) => (i === 1 ? { ...l, depth: { ...l.depth, thickness: 0.3 } } : l))
  const kept = keepStack(stack, after, H, H)
  assert.deepEqual(kept.map((l) => l.depth.z), [0, 0.29, 0.29 + 0.4 + 0.03])
  // reorder / delete keep the stack too; a custom stack is returned untouched
  const del = keepStack(stack, [stack[0], stack[2]], H, H)
  assert.deepEqual(del.map((l) => l.depth.z), [0, 0.29])
  const custom = mk([0, 0.4, 0.6])
  const edited = custom.map((l) => ({ ...l, depth: { ...l.depth, inflate: 1 } }))
  assert.equal(keepStack(custom, edited, H, H), edited)
  // which edits can move the stack: heights, order, layer set, scale — never a plain z drag
  assert.equal(stackAffected(stack, stack, 1, 1), false)
  assert.equal(stackAffected(stack, after, 1, 1), true)
  assert.equal(stackAffected(stack, [stack[1], stack[0], stack[2]], 1, 1), true)
  assert.equal(stackAffected(stack, stack, 1, 1.2), true)
  const zOnly = stack.map((l, i) => (i === 2 ? { ...l, depth: { ...l.depth, z: 0.9 } } : l))
  assert.equal(stackAffected(stack, zOnly, 1, 1), false)
  // the editor keeps the stack on every commit: the editor page installs keepProjectStack as the store's commit
  // transform (injected, so the store — loaded by the home page too — does not bundle the 3D geometry code)
  const store = read('src/store/editor.ts')
  assert.match(store, /raw !== s\.project && commitTransform \? commitTransform\(s\.project, raw, s\.geometry\) : raw/)
  const imports = store.split(/\r?\n/).filter((l) => l.startsWith('import')).join(' ')
  assert.ok(!/viewport|stacking/.test(imports), 'no 3D imports in the store')
  const stacking = read('src/features/editor/stacking.ts')
  assert.match(stacking, /setCommitTransform\(\(before, after, geometry\) => keepProjectStack\(before, after, geometry, useAppStore\.getState\(\)\.presets\.data\)\)/)
  assert.match(read('src/features/editor/EditorPage.tsx'), /^installStackKeeper\(\)$/m)
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

// Round-4 UI polish regression tests: the live viewport's backdrop blends into the editor stage (no dark box), renders
// are never shown above 1:1 by default, Apple's names ("Default", "Cycles" / "EEVEE"), element counts and the
// full-bleed badge.
// Run (from web/): node --test src/viewport/tests/*.test.mjs   (Node ≥ 23.6: built-in TS type stripping)
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

const WEB = new URL('../../../', import.meta.url)
const read = (p) => readFileSync(new URL(p, WEB), 'utf8')

const dt = await import('../scene/displayTransform.ts')
const stage = await import('../../lib/stageBackdrop.ts')
const labels = await import('../../lib/labels.ts')
const frame = await import('../../features/editor/stage/frame.ts')
const ops = await import('../../lib/projectOps.ts')
const pick = await import('../../lib/renderPick.ts')

const s2l = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)
const l2s = (c) => (c <= 0.0031308 ? c * 12.92 : 1.055 * Math.max(0, c) ** (1 / 2.4) - 0.055)
const MODES = ['brand', 'neutral', 'standard', 'agx', 'agx-punchy']

/** What the viewport shows (sRGB 0..255) when the backdrop paints `css` (sRGB 0..1) the way Backdrop.tsx does. */
function shown(css, colorMode) {
  const t = dt.displayTransformFor(colorMode)
  const scene = dt.toScene(css.map(s2l), t)
  return dt.toDisplay(scene, t).map((v) => l2s(v) * 255)
}

// ------------------------------------------------------------------------------------------------ 1 · backdrop
test('display transform mirrors three.js tone mapping constants and Effects.tsx', () => {
  const src = read('node_modules/three/src/renderers/shaders/ShaderChunk/tonemapping_pars_fragment.glsl.js')
  const nums = (name) => {
    const m = new RegExp(`${name}\\s*=\\s*mat3\\(([\\s\\S]*?)\\);`).exec(src)
    assert.ok(m, `${name} not found in three`)
    return m[1].replace(/vec3\(/g, '(').match(/-?\s*[0-9.]+(?:e-?\d+)?/g).map((v) => Number(v.replace(/\s+/g, '')))
  }
  assert.deepEqual(dt.LINEAR_SRGB_TO_LINEAR_REC2020.flat(), nums('LINEAR_SRGB_TO_LINEAR_REC2020'))
  assert.deepEqual(dt.LINEAR_REC2020_TO_LINEAR_SRGB.flat(), nums('LINEAR_REC2020_TO_LINEAR_SRGB'))
  assert.deepEqual(dt.AGX_INSET.flat(), nums('AgXInsetMatrix'))
  assert.deepEqual(dt.AGX_OUTSET.flat(), nums('AgXOutsetMatrix'))
  assert.equal(dt.AGX_MIN_EV, Number(/AgxMinEv\s*=\s*(-\s*[0-9.]+)/.exec(src)[1].replace(/\s+/g, '')))
  assert.equal(dt.AGX_MAX_EV, Number(/AgxMaxEv\s*=\s*([0-9.]+)/.exec(src)[1]))
  // Effects.tsx picks the tone mapper per colour mode exactly as displayTransformFor() assumes.
  const fx = read('src/viewport/scene/Effects.tsx')
  assert.match(fx, /colorMode === 'standard'\s*\?\s*ToneMappingMode\.LINEAR/)
  assert.match(fx, /colorMode === 'agx' \|\| colorMode === 'agx-punchy'\s*\?\s*ToneMappingMode\.AGX\s*:\s*ToneMappingMode\.NEUTRAL/)
  assert.match(fx, /saturation=\{colorMode === 'agx-punchy' \? AGX_PUNCHY_SATURATION : 0\}/)
  // The shader twin carries the same numbers.
  for (const m of [dt.AGX_INSET, dt.AGX_OUTSET, dt.LINEAR_SRGB_TO_LINEAR_REC2020]) {
    for (const v of m.flat()) assert.ok(dt.DISPLAY_INVERSE_GLSL.includes(String(v)), `GLSL lacks ${v}`)
  }
})

test('backdrop scene colours come out of every tone mapper as the CSS stage colours (no box around the live view)', () => {
  const W = 1028
  const H = 928
  for (const mode of MODES) {
    for (const [x, y] of [[0, 0], [150, 64], [514, 64], [900, 400], [514, 800], [11, 11]]) {
      const css = stage.stageColorAt(x, y, W, H)
      const out = shown(css, mode)
      const want = css.map((v) => v * 255)
      for (let i = 0; i < 3; i++) assert.ok(Math.abs(out[i] - want[i]) < 0.3, `${mode} @${x},${y}: ${out} vs ${want}`)
    }
    // Checker centre colours too (both cells, full checker opacity).
    for (const dx of [0, 6]) {
      const css = stage.checkerOver(stage.stageColorAt(500, 400, W, H), 300 + dx + 0.5, 300.5, 600, 600)
      const out = shown(css, mode)
      css.forEach((v, i) => assert.ok(Math.abs(out[i] - v * 255) < 0.3, `${mode} checker ${out} vs ${css}`))
    }
  }
  // Round 3's bug for reference: the raw #17171c checker through Khronos PBR Neutral lands ~10 levels below the stage.
  const t = dt.displayTransformFor('neutral')
  const old = dt.toDisplay([0x17, 0x17, 0x1c].map((v) => s2l(v / 255)), t).map((v) => l2s(v) * 255)
  assert.ok(old[0] < 3 && old[2] < 13, `old checker shows as ${old}`)
})

test('AgX inverse converges for the whole backdrop range', () => {
  const t = dt.displayTransformFor('agx')
  for (let r = 8; r <= 40; r += 4) {
    for (const [g, b] of [[r, r + 4], [r - 1, r + 10]]) {
      const want = [r, g, b].map((v) => s2l(v / 255))
      const back = dt.toDisplay(dt.toScene(want, t), t)
      back.forEach((v, i) => assert.ok(Math.abs(v - want[i]) < 1e-6, `${[r, g, b]}: ${back} vs ${want}`))
    }
  }
})

test('stage CSS is generated from the one spec the shader uses (index.css no longer defines it)', () => {
  const css = stage.stageBackgroundStyle()
  const S = stage.STAGE_BACKDROP
  assert.equal(css.backgroundColor, `rgb(${S.base.join(' ')})`)
  assert.match(css.backgroundImage, /radial-gradient\(ellipse 80% 70% at 50% 42%, rgb\(143 125 255 \/ 0\.07\), transparent 70%\)/)
  assert.match(css.backgroundImage, /rgb\(255 255 255 \/ 0\.035\) 1px, transparent 1\.2px/)
  assert.equal(css.backgroundSize, '100% 100%, 22px 22px')
  const chk = stage.checkerOverlayStyle()
  assert.equal(chk.opacity, 0.4)
  assert.equal(chk.backgroundSize, '12px 12px')
  assert.match(chk.maskImage, /closest-side/)
  assert.doesNotMatch(read('src/index.css'), /@utility stage-backdrop/)
  const shader = read('src/viewport/scene/Backdrop.tsx')
  assert.match(shader, /uniform vec4 uStage;/)
  assert.match(shader, /STAGE_BACKDROP/)
  // The checker phase: the frame's top-left cell is `b`, its right neighbour `a` (same in CSS and shader).
  const base = [0, 0, 0]
  const c = 6 * 83333 // a cell corner at the centre of a huge frame (mask ≈ 1 for both cells)
  const tl = stage.checkerOver(base, c + 0.5, c + 0.5, 2 * c, 2 * c)
  const tr = stage.checkerOver(base, c + 6.5, c + 0.5, 2 * c, 2 * c)
  assert.ok(Math.abs((tl[0] * 255) / S.checker.opacity - S.checker.b[0]) < 0.01, `${tl}`)
  assert.ok(Math.abs((tr[0] * 255) / S.checker.opacity - S.checker.a[0]) < 0.01, `${tr}`)
})

test('the dot grid matches CSS: a 2×2 px dot per 22 px tile at alpha .035', () => {
  const S = stage.STAGE_BACKDROP
  const base = S.base[0] / 255
  const at = (x, y) => stage.stageColorAt(x, y, 1e6, 1e6, { ...S, glow: { ...S.glow, alpha: 0 } })[0]
  assert.ok(Math.abs(at(10.5, 10.5) - (base + (1 - base) * 0.035)) < 1e-9)
  assert.ok(Math.abs(at(11.5, 11.5) - (base + (1 - base) * 0.035)) < 1e-9)
  assert.equal(at(12.5, 12.5), base)
  assert.equal(at(0.5, 0.5), base)
})

// ------------------------------------------------------------------------------------------------ 2 · 1:1 renders
test('Fit never shows a render above its native pixels; zoom and Actual pixels still go past', () => {
  // QA round 3: a 512 px preview in a 738 px fitted frame at 100 %.
  assert.equal(frame.frameSide(738, 1, 1, null), 738) // live view: unchanged
  assert.equal(frame.frameSide(738, 1, 1, 512), 512) // render: 1:1, centred
  assert.equal(frame.frameSide(738, 1, 2, 512), 256) // HiDPI: 512 device px
  assert.equal(frame.frameSide(738, 1, 1.25, 512), 409.6)
  assert.equal(frame.frameSide(400, 1, 1, 1024), 400) // larger than the stage: fit
  assert.equal(frame.frameSide(738, 2, 1, 512), 1024) // explicit zoom past native
  assert.equal(frame.frameSide(738, 0.5, 1, 512), 256)
  assert.equal(frame.actualPixelsZoom(738, 1, 512), 1)
  assert.equal(frame.actualPixelsZoom(400, 1, 1024), 2.56)
  assert.equal(frame.pixelScaleLabel(1), '1:1')
  assert.equal(frame.pixelScaleLabel(1.4414), '144%')
  assert.equal(frame.pixelScaleLabel(0.5), '50%')
  for (const dpr of [1, 1.25, 1.5, 2]) {
    const side = frame.frameSide(1000, 1, dpr, 512)
    assert.ok(Math.abs(side * dpr - 512) < 1e-9, `dpr ${dpr}: ${side}`)
  }
  // At 125 % / 150 % the box lays out 1/64 px short of N device px (170.65625 CSS px = 255.98 device px at 1.5×), and
  // smooth sampling re-filters the whole render (measured mean |Δ| 0.48-0.94 levels, max 76-80 vs the PNG); the 1:1
  // image is drawn nearest-neighbour instead (measured exact: mean 0, max 0).
  assert.equal(frame.isOneToOne(1), true)
  assert.equal(frame.isOneToOne(255.984 / 256), true)
  assert.equal(frame.isOneToOne(1.44), false)
  assert.equal(frame.isOneToOne(null), false)
  const stageSrc = read('src/features/editor/stage/Stage.tsx')
  assert.equal((stageSrc.match(/<RenderImage url=\{entry\.url\} className="absolute inset-0" pixelExact=\{isOneToOne\(scale\)\} \/>/g) ?? []).length, 2) // Render + Compare
  assert.match(read('src/features/editor/stage/RenderImage.tsx'), /pixelExact && l\.url === url \? \{ imageRendering: 'pixelated'/)
})

test('a smaller renditions-strip render of the same state does not replace the shown (sharper) render', () => {
  const shown = { quality: 'preview', state: 'done', url: '/r/a.png', width: 512, sig: 'S1' }
  const rend = (o) => ({ quality: 'preview', state: 'done', url: '/r/b.png', width: 256, sig: 'S1', purpose: 'rendition', ...o })
  assert.equal(pick.keepsSharperRender(shown, rend({})), true) // "Render all" after the 512 px preview: keep 512
  assert.equal(pick.keepsSharperRender(shown, rend({ sig: 'S2' })), false) // newer state: replace
  assert.equal(pick.keepsSharperRender({ ...shown, quality: 'draft' }, rend({})), false) // better tier: replace
  assert.equal(pick.keepsSharperRender(shown, rend({ purpose: 'live' })), false) // only renditions are held back
  assert.equal(pick.keepsSharperRender(shown, rend({ width: 512 })), false)
  assert.equal(pick.keepsSharperRender(undefined, rend({})), false)
  assert.match(read('src/store/render.ts'), /newer\(s\.latest\[appearance\]\) && !keepsSharperRender\(shown, entry\)/)
})

// ------------------------------------------------------------------------------------------------ 3 · names
test('the base appearance is "Default" everywhere; engines are "Cycles" / "EEVEE"', () => {
  const presets = JSON.parse(read('../shared/presets.json'))
  assert.equal(labels.appearanceLabel('light', presets), 'Default')
  assert.equal(labels.appearanceLabel('light', presets, 'short'), 'Default')
  assert.equal(labels.appearanceLabel('clear-dark', presets), 'Clear Dark')
  assert.equal(labels.appearanceLabel('tinted-light', null), 'Tinted Light')
  assert.equal(labels.appearanceLabel('dark', null, 'short'), 'Dark')
  for (const [raw, want] of [['cycles', 'Cycles'], ['CYCLES', 'Cycles'], ['eevee', 'EEVEE'], ['BLENDER_EEVEE_NEXT', 'EEVEE'], ['BLENDER_EEVEE', 'EEVEE']])
    assert.equal(labels.engineLabel(raw), want)
  assert.equal(labels.engineLabel(undefined, 'draft'), 'EEVEE')
  assert.equal(labels.engineLabel(null, 'preview'), 'Cycles')
  // No surface reads presets' appearance names directly any more (they'd say "Light").
  for (const f of [
    'src/features/editor/TopBar.tsx',
    'src/features/editor/stage/RenditionsStrip.tsx',
    'src/features/editor/stage/MatrixView.tsx',
    'src/features/editor/stage/Stage.tsx',
    'src/features/editor/StatusBar.tsx',
    'src/features/dialogs/ExportDialog.tsx',
  ]) {
    assert.doesNotMatch(read(f), /presets\??\.appearances\[/, f)
    assert.doesNotMatch(read(f), /\.engine \?\? \(/, f)
  }
  assert.match(read('src/store/render.ts'), /engine: typeof r\.engine === 'string' \? engineLabel\(r\.engine, quality\)/)
  // The manual-render toast names the appearance too ("Default · 512 px", not "light · 512 px").
  assert.match(read('src/store/render.ts'), /description: `\$\{appearanceLabel\(appearance, /)
  assert.doesNotMatch(read('src/store/render.ts'), /description: `\$\{appearance\} ·/)
})

// ------------------------------------------------------------------------------------------------ 4 · matrix toolbar
test('Matrix view hides the stage controls that do nothing there', () => {
  const src = read('src/features/editor/stage/Stage.tsx')
  const toolbar = src.slice(src.indexOf('function StageToolbar'), src.indexOf('function LightControl'))
  const gate = toolbar.indexOf("{mode !== 'matrix' && (")
  assert.ok(gate > 0)
  // the CAD view angle (PLAN §11, replaced Explode) is a head-on <-> isometric camera: the matrix renders are head-on
  for (const ctl of ['<ViewAngleControl', 'label="Icon grid"', 'label="Zoom out"', '<LightControl']) {
    assert.ok(toolbar.indexOf(ctl) > gate, `${ctl} is not behind the matrix gate`)
  }
  const keys = read('src/features/editor/shortcuts.ts')
  assert.match(keys, /case 'g':\s*\n\s*if \(ui\.stageMode === 'matrix'\) return/)
  assert.match(keys, /case 'i':\s*\n\s*if \(ui\.stageMode === 'matrix'\) return/)
})

// ------------------------------------------------------------------------------------------------ 5 · element counts
test('element count = elements in layers; the plate element is counted under Canvas & plate', () => {
  // Photos as imported (real numbers): e0 is the detected plate (art bbox ±0.932, art.scale 1.072957 → canvas ±1),
  // one petal per layer.
  const k = 1.072957
  const el = (id, bbox) => ({ id, bbox })
  const petal = [0, 0, 0.664, 0.338]
  const photos = {
    elements: [el('e0', [-1 / k, -1 / k, 1 / k, 1 / k]), el('e1', petal), el('e2', petal), el('e3', petal), el('e4', petal)],
    layers: [['e1'], ['e2'], ['e3'], ['e4']].map((ids) => ({ elementIds: ids })),
    canvas: { art: { scale: k, x: 0, y: 0 } },
    source: { plateDetected: true },
  }
  assert.deepEqual(ops.elementCounts(photos), { layered: 4, plate: 1, unused: 0 })
  // Deleting layers leaves their elements in project.elements (removeLayers): they are NOT the plate.
  const deleted = ops.removeLayers({ ...photos, layers: photos.layers.map((l, i) => ({ ...l, id: `L${i}` })), appearances: { dark: { layers: {} }, mono: { layers: {} } } }, ['L2', 'L3'])
  assert.deepEqual(ops.elementCounts(deleted), { layered: 2, plate: 1, unused: 2 })
  // Full-bleed art (Earth): no plate element, so an element left in no layer is never "the plate".
  const earth = { ...photos, elements: [el('e0', [-0.93, -0.8, 0.91, 0.45]), el('e1', petal)], layers: [{ elementIds: ['e1'] }], source: { plateDetected: false } }
  assert.deepEqual(ops.elementCounts(earth), { layered: 1, plate: 0, unused: 1 })
  // Duplicate / dangling ids never inflate the count.
  const odd = { ...photos, elements: [photos.elements[0], el('e1', petal)], layers: [{ elementIds: ['e1', 'e1', 'ghost'] }] }
  assert.deepEqual(ops.elementCounts(odd), { layered: 1, plate: 1, unused: 0 })
  assert.deepEqual(ops.elementCounts({ elements: [], layers: [] }), { layered: 0, plate: 0, unused: 0 })
  assert.match(read('src/features/editor/StatusBar.tsx'), /in no layer \(deleted layers\)/)
  assert.match(read('src/features/editor/StatusBar.tsx'), /elementCounts\(s\.project\)/)
  assert.match(read('src/features/editor/layers/LayersPanel.tsx'), /elementCounts\(project\)\.plate/)
})

// ------------------------------------------------------------------------------------------------ 6 · full-bleed
test('full-bleed sources get a "Full-bleed" badge instead of "No plate"', () => {
  assert.equal(labels.sourcePlateKind({ plateDetected: false, fullBleed: true }), 'full-bleed')
  assert.equal(labels.sourcePlateKind({ plateDetected: true, fullBleed: true }), 'full-bleed')
  assert.equal(labels.sourcePlateKind({ plateDetected: true }), 'plate')
  assert.equal(labels.sourcePlateKind({ plateDetected: false }), 'none')
  assert.match(read('src/types.ts'), /fullBleed\?: boolean/)
  assert.match(read('src/features/editor/inspector/DocumentInspector.tsx'), /<SourcePlateBadge source=\{project\.source\} \/>/)
})

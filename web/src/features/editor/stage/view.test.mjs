// CAD-style stage navigation: zoom at the cursor, pan, one view shared by Live / Render / Compare, the X key
// (top-down ↔ isometric) and its undo behaviour. Run (from web/): node --test src/features/editor/stage/view.test.mjs
import '../../../viewport/tests/helpers.mjs' // registers the extensionless .ts resolver
import { register } from 'node:module'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

// directory imports ('../api') resolve to their index.ts
register(
  'data:text/javascript,' +
    encodeURIComponent(`
export async function resolve(spec, ctx, next) {
  try {
    return await next(spec, ctx)
  } catch (err) {
    if (spec.startsWith('.') && !/\\.[cm]?[jt]sx?$/.test(spec)) return next(spec + '/index.ts', ctx)
    throw err
  }
}`),
  import.meta.url,
)

// ---------------------------------------------------------------- minimal browser globals for the stores
const listeners = {}
const timers = []
globalThis.window = {
  addEventListener: (type, fn) => (listeners[type] ??= []).push(fn),
  removeEventListener() {},
  setTimeout: (fn, ms = 0) => (timers.push({ fn, ms }), timers.length),
  clearTimeout() {},
  setInterval: () => 0,
  clearInterval() {},
  location: { hash: '', protocol: 'http:', host: 'localhost' },
}
const fire = (type) => (listeners[type] ?? []).forEach((fn) => fn({ type }))
/** Runs the zero-delay timers (gesture end); drops the debounced autosaves. */
const flushTimers = () => timers.splice(0).forEach((t) => t.ms === 0 && t.fn())
const frames = new Map()
let frameId = 0
globalThis.requestAnimationFrame = (fn) => (frames.set(++frameId, fn), frameId)
globalThis.cancelAnimationFrame = (id) => frames.delete(id)
/** Runs animation frames `ms` later than now until none are pending. */
function runFrames(ms = 1000) {
  for (let guard = 0; frames.size && guard < 1000; guard++) {
    const due = [...frames.values()]
    frames.clear()
    due.forEach((fn) => fn(performance.now() + ms))
  }
}

const view = await import('./view.ts')
const read = (p) => readFileSync(new URL(p, import.meta.url), 'utf8')
const near = (a, b, eps, msg = '') => assert.ok(Math.abs(a - b) <= eps, `${msg} ${a} ≉ ${b} (±${eps})`)

const W = 1028
const H = 928
const live = view.stageLayout(W, H, 1, null)
const render = view.stageLayout(W, H, 1, 512) // a 512 px preview: Fit shows it 1:1

/** Art point (frame-relative, 0..1) under the area point (px, py). */
const artAt = (v, L, px, py) => {
  const r = view.frameRect(v, L)
  return [(px - r.x) / r.side, (py - r.y) / r.side]
}

// ---------------------------------------------------------------- layout / Fit
test('Fit places the frame below the toolbar like before; Render Fit is at most 1:1', () => {
  assert.equal(live.fit, 824) // min(1028 − 2·40, 928 − 64 − 40)
  const r = view.frameRect(null, live)
  assert.deepEqual(r, { x: 102, y: 64, side: 824 }) // 64 px for the toolbar, 40 px margin below
  const rr = view.frameRect(null, render)
  assert.equal(rr.side, 512) // QA round 3: 1:1, centred
  near(rr.x + rr.side / 2, r.x + r.side / 2, 0.5)
  near(rr.y + rr.side / 2, r.y + r.side / 2, 0.5)
  assert.equal(view.zoomPercent(null, live), 100)
  assert.equal(view.zoomPercent(null, render), 62) // 512 / 824
  // HiDPI: whole device pixels
  const hd = view.stageLayout(1001, 777, 1.5, null)
  const h = view.frameRect({ zoom: 1.37, x: 0.123, y: -0.071 }, hd)
  for (const v of [h.x, h.y, h.side]) near(v * 1.5, Math.round(v * 1.5), 1e-9)
})

// ---------------------------------------------------------------- zoom at the cursor
test('wheel zoom keeps the point under the cursor fixed and is clamped to 25 %–800 %', () => {
  let v = null
  const cursor = [301, 517]
  const before = artAt(v, live, ...cursor)
  for (let i = 0; i < 6; i++) {
    v = view.zoomAt(v, view.wheelZoomFactor({ deltaY: -100, deltaMode: 0 }), ...cursor, live)
    const a = artAt(v, live, ...cursor)
    // within a device pixel of the frame (snapping), at any zoom
    const side = view.frameRect(v, live).side
    near(a[0] * side, before[0] * side, 1, `zoom ${v.zoom} x`)
    near(a[1] * side, before[1] * side, 1, `zoom ${v.zoom} y`)
  }
  assert.ok(v.zoom > 3 && v.zoom < 3.5, `six notches ≈ ×1.22⁶: ${v.zoom}`)
  // zooming back out by the same notches returns to Fit (pan included)
  for (let i = 0; i < 6; i++) v = view.zoomAt(v, view.wheelZoomFactor({ deltaY: 100, deltaMode: 0 }), ...cursor, live)
  near(v.zoom, 1, 1e-9)
  near(v.x, 0, 1e-9)
  near(v.y, 0, 1e-9)
  // limits
  let w = null
  for (let i = 0; i < 60; i++) w = view.zoomAt(w, 1.5, 500, 400, live)
  assert.equal(w.zoom, view.MAX_ZOOM)
  assert.equal(view.MAX_ZOOM, 8)
  for (let i = 0; i < 60; i++) w = view.zoomAt(w, 1 / 1.5, 500, 400, live)
  assert.equal(w.zoom, view.MIN_ZOOM)
  assert.equal(view.MIN_ZOOM, 0.25)
  // trackpad pinch (ctrl+wheel, small pixel deltas) zooms too; lines are converted to pixels
  assert.ok(view.wheelZoomFactor({ deltaY: -10, deltaMode: 0, ctrlKey: true }) > 1.1)
  near(view.wheelZoomFactor({ deltaY: 3, deltaMode: 1 }), view.wheelZoomFactor({ deltaY: 48, deltaMode: 0 }), 1e-12)
  assert.equal(view.isNotchWheel({ deltaY: 100, deltaMode: 0 }), true)
  assert.equal(view.isNotchWheel({ deltaY: 4, deltaMode: 0 }), false) // trackpad scroll: applied at once
  assert.equal(view.isNotchWheel({ deltaY: -8, deltaMode: 0, ctrlKey: true }), false)
})

// ---------------------------------------------------------------- pan
test('middle-drag pan moves the frame with the pointer and never loses it', () => {
  const v0 = view.zoomAt(null, 3, 400, 300, live)
  const r0 = view.frameRect(v0, live)
  const v1 = view.panBy(v0, 37, -21, live)
  const r1 = view.frameRect(v1, live)
  near(r1.x - r0.x, 37, 1)
  near(r1.y - r0.y, -21, 1)
  assert.equal(r1.side, r0.side) // pan never zooms
  // far away: at least 64 px of the frame stay on the stage
  const lost = view.frameRect(view.panBy(null, 5000, -5000, live), live)
  assert.ok(lost.x <= W - 64 + 1 && lost.y + lost.side >= 64 - 1, JSON.stringify(lost))
  // resize-proof: the view is stored in units of the fitted side
  const big = view.stageLayout(W * 2, H * 2, 1, null)
  const rb = view.frameRect(v1, big)
  near(rb.side / big.fit, r1.side / live.fit, 0.01)
})

test('one view for Live, Render and Compare: switching views keeps the same region', () => {
  const v = view.panBy(view.zoomAt(null, 2.6, 250, 640, live), -80, 45, live)
  assert.deepEqual(view.frameRect(v, live), view.frameRect(v, render))
  // the toolbar's buttons / menu zoom about the stage centre and stay inside the limits
  const z = view.zoomTo(v, 99, live)
  assert.equal(z.zoom, view.MAX_ZOOM)
  // the Fit view becomes an explicit one when first zoomed / panned (Render: from its 1:1 size)
  near(view.viewOf(null, render).zoom, 512 / 824, 1e-12)
  near(view.frameRect(view.panBy(null, 0, 0, render), render).side, 512, 1e-9)
})

// ---------------------------------------------------------------- X: top-down ↔ iso, undo
const { useEditor } = await import('../../../store/editor.ts')
const { useUi } = await import('../../../store/ui.ts')
const actions = await import('../actions.ts')

function openProject(iso = 0) {
  const project = {
    id: 'p1', name: 't', version: 1, layers: [], elements: [],
    camera: { view: 'front', tiltX: 0, tiltY: 0, fov: 30, zoom: 1, iso, explode: 1 },
    canvas: { platform: 'ios', shape: 'squircle', cornerRadius: 0.2, plate: {}, art: { scale: 1, x: 0, y: 0 } },
    appearance: 'light',
  }
  useEditor.setState({ project, status: 'ready', past: [], future: [], busy: null })
  useUi.setState({ isoAnim: null })
  fire('pointerup') // no gesture in flight
  flushTimers()
}
const iso = () => useEditor.getState().project.camera.iso
const steps = () => useEditor.getState().past.length
/** The View slider: a pointer gesture with several changes. */
function sliderDrag(...values) {
  fire('pointerdown')
  for (const v of values) actions.setIso(v)
  fire('pointerup')
  flushTimers()
}

test('X swings top-down ↔ isometric as ONE undo step per press', () => {
  openProject(0)
  actions.animateIso()
  assert.ok(useUi.getState().isoAnim === 0 && iso() === 0, 'animating; nothing committed mid-swing')
  runFrames()
  assert.equal(iso(), 1)
  assert.equal(steps(), 1)
  assert.equal(useUi.getState().isoAnim, null)
  // a second press right away (well within the 1 s coalescing window) is its own step
  actions.animateIso()
  runFrames()
  assert.equal(iso(), 0)
  assert.equal(steps(), 2)
  useEditor.getState().undo()
  assert.equal(iso(), 1)
  useEditor.getState().undo()
  assert.equal(iso(), 0)
})

test('X then dragging the View slider = two undo steps (pointerdown starts a fresh commit)', () => {
  openProject(0)
  actions.animateIso()
  runFrames()
  sliderDrag(0.8, 0.6, 0.55)
  assert.equal(iso(), 0.55)
  assert.equal(steps(), 2)
  useEditor.getState().undo()
  assert.equal(iso(), 1, 'undo reverts only the drag')
  useEditor.getState().undo()
  assert.equal(iso(), 0)
  // a slider drag is still one step however many changes it makes, and the next drag is a new one
  openProject(0)
  sliderDrag(0.1, 0.2, 0.3, 0.4)
  sliderDrag(0.5, 0.6)
  assert.equal(steps(), 2)
  // a keyboard step on the focused slider, then a drag right after: two steps (it merged before)
  openProject(0)
  actions.setIso(0.01)
  sliderDrag(0.3, 0.4)
  assert.equal(steps(), 2)
  // pressing X mid-swing reverses it without committing the interrupted swing
  openProject(0)
  actions.animateIso()
  const due = [...frames.values()]
  frames.clear()
  due.forEach((fn) => fn(performance.now() + 200)) // part-way
  assert.ok(useUi.getState().isoAnim > 0 && useUi.getState().isoAnim < 1)
  actions.animateIso()
  runFrames()
  assert.equal(iso(), 0)
  assert.equal(steps(), 0)
})

test('X is bound once (Explode is gone), I stays as an alias, both ignored in the matrix and while typing', () => {
  const keys = read('../shortcuts.ts')
  assert.match(keys, /case 'x':[^\n]*\n\s*case 'i':\s*\n\s*if \(ui\.stageMode === 'matrix'\) return/)
  assert.equal((keys.match(/case 'x'/g) ?? []).length, 1)
  assert.match(keys, /if \(e\.defaultPrevented \|\| isTypingTarget\(e\.target\)\) return/)
  assert.ok(!/explode/i.test(keys))
  assert.match(keys, /keys: 'X', label: 'Top-down ↔ isometric/)
  assert.match(keys, /keys: 'Wheel'/)
  assert.match(keys, /keys: 'Middle-drag'/)
  assert.match(keys, /keys: '0', label: 'Reset view/)
  const stage = read('./Stage.tsx')
  assert.equal((stage.match(/ kbd="X"/g) ?? []).length, 2) // Top-down / Isometric buttons
  assert.match(stage, /data-tip-kbd="X"/) // the View slider
  assert.ok(!/kbd="I"/.test(stage))
  // the wheel listener is non-passive and in the capture phase (no page scroll / zoom, no OrbitControls dolly)
  assert.match(read('./viewControl.ts'), /addEventListener\('wheel', onWheel, \{ passive: false, capture: true \}\)/)
})

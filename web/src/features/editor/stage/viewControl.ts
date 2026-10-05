// Stage navigation (CAD-style): wheel / pinch zooms at the cursor, middle-drag (or Space + drag) pans, a double
// middle-click / 0 / Home / Mod+0 resets to Fit. The view lives in useUi.stageView (view.ts has the pure math); the
// Live, Render and Compare views all place the icon frame from it.
import { useEffect } from 'react'
import { isTypingTarget } from '../../../lib/hooks'
import { useUi } from '../../../store/ui'
import {
  clampZoom,
  isNotchWheel,
  lerpView,
  MAX_ZOOM,
  MIN_ZOOM,
  panBy,
  sameView,
  viewOf,
  wheelZoomFactor,
  zoomAt,
  zoomTo,
  type StageLayout,
  type StageView,
} from './view'

let layout: StageLayout | null = null

/** The stage's current geometry (Stage.tsx keeps it current); the actions below need it to place the frame. */
export function setStageLayout(l: StageLayout): void {
  layout = l
}

const getView = () => useUi.getState().stageView
const putView = (v: StageView | null) => {
  if (!sameView(getView(), v)) useUi.setState({ stageView: v })
}

// ------------------------------------------------------------------------------------------ eased transitions
type Anim =
  | { kind: 'wheel'; pending: number; ax: number; ay: number; last: number }
  | { kind: 'to'; from: StageView; to: StageView; fit: boolean; t0: number; dur: number }

let anim: Anim | null = null
let raf = 0

export function stopViewAnimation(): void {
  if (raf) cancelAnimationFrame(raf)
  raf = 0
  anim = null
}

function tick(t: number) {
  raf = 0
  const L = layout
  const a = anim
  if (!L || !a) return void (anim = null)
  if (a.kind === 'wheel') {
    const dt = Math.min(0.05, Math.max(0.008, (t - a.last) / 1000))
    a.last = t
    let step = a.pending * (1 - Math.exp(-dt * 22))
    if (Math.abs(a.pending - step) < 2e-3) step = a.pending
    a.pending -= step
    putView(zoomAt(getView(), Math.exp(step), a.ax, a.ay, L))
    if (a.pending === 0) return void (anim = null)
  } else {
    const k = Math.min(1, Math.max(0, (t - a.t0) / a.dur))
    const e = 1 - Math.pow(1 - k, 3)
    if (k >= 1) {
      anim = null
      return putView(a.fit ? null : a.to)
    }
    putView(lerpView(a.from, a.to, e))
  }
  raf = requestAnimationFrame(tick)
}

function animateTo(target: StageView | null) {
  const L = layout
  if (!L) return putView(target)
  const from = viewOf(getView(), L)
  const to = viewOf(target, L)
  stopViewAnimation()
  if (sameView(from, to)) return putView(target)
  anim = { kind: 'to', from, to, fit: target === null, t0: performance.now(), dur: 240 }
  raf = requestAnimationFrame(tick)
}

/** The view the running transition ends at (so repeated button presses compound instead of undershooting). */
function settledView(): StageView | null {
  if (anim?.kind === 'to') return anim.fit ? null : anim.to
  return getView()
}

// ------------------------------------------------------------------------------------------ actions
/** Fit: every view shows the whole icon again (eased). */
export function resetView(animate = true): void {
  if (!animate) {
    stopViewAnimation()
    return putView(null)
  }
  animateTo(null)
}

let viewProject: string | null = null

/** Opening a different project starts at Fit (the view is navigation state, not part of a project). */
export function resetViewForProject(id: string | null | undefined): void {
  if (!id || id === viewProject) return
  if (viewProject !== null) resetView(false)
  viewProject = id
}

/** Zoom to `zoom` (1 = 100 % = the fitted frame) about the stage centre (toolbar menu). */
export function zoomViewTo(zoom: number): void {
  if (!layout) return
  animateTo(zoomTo(settledView(), zoom, layout))
}

/** Zoom in (+1) / out (−1) one step about the stage centre (toolbar buttons). */
export function zoomViewStep(dir: 1 | -1): void {
  if (!layout) return
  const v = viewOf(settledView(), layout)
  animateTo(zoomTo(v, clampZoom(v.zoom * Math.pow(1.25, dir)), layout))
}

/** One wheel / pinch event at area point (px, py): mouse notches ease in, trackpad deltas apply at once. */
export function wheelZoom(e: { deltaY: number; deltaMode: number; ctrlKey: boolean }, px: number, py: number): void {
  const L = layout
  if (!L || !e.deltaY) return
  const factor = wheelZoomFactor(e, L.height)
  if (!isNotchWheel(e)) {
    stopViewAnimation()
    return putView(zoomAt(getView(), factor, px, py, L))
  }
  if (anim?.kind !== 'wheel') {
    stopViewAnimation()
    anim = { kind: 'wheel', pending: 0, ax: px, ay: py, last: performance.now() }
  }
  const z = viewOf(getView(), L).zoom
  // never queue zoom beyond the limits (reversing at the limit would first have to burn that off)
  anim.pending = Math.min(Math.log(MAX_ZOOM / z), Math.max(Math.log(MIN_ZOOM / z), anim.pending + Math.log(factor)))
  anim.ax = px
  anim.ay = py
  if (!raf) raf = requestAnimationFrame(tick)
}

/** Pan by (dx, dy) CSS px. */
export function panView(dx: number, dy: number): void {
  if (!layout || (!dx && !dy)) return
  stopViewAnimation()
  putView(panBy(getView(), dx, dy, layout))
}

// ------------------------------------------------------------------------------------------ DOM wiring
/**
 * Wires the stage: `area` (the stage area incl. the floating toolbar) takes the wheel (always, so the page itself
 * never scrolls or browser-zooms over the stage); `surface` (the views) takes middle-drag / Space + drag pans and the
 * double middle-click. Listeners run in the capture phase and stop the event, so a pan never reaches the three.js
 * canvas (no layer select / drag, no OrbitControls) or the Compare divider.
 */
export function useStageNavigation(area: HTMLElement | null, surface: HTMLElement | null): void {
  useEffect(() => {
    if (!area) return
    const onWheel = (e: WheelEvent) => {
      if (useUi.getState().stageMode === 'matrix') {
        if (e.ctrlKey || e.metaKey) e.preventDefault() // the matrix scrolls; never browser-zoom the app
        return
      }
      e.preventDefault()
      e.stopPropagation()
      const r = area.getBoundingClientRect()
      wheelZoom(e, e.clientX - r.left, e.clientY - r.top)
    }
    area.addEventListener('wheel', onWheel, { passive: false, capture: true })
    return () => area.removeEventListener('wheel', onWheel, { capture: true })
  }, [area])

  useEffect(() => {
    if (!surface) return
    let pan: { id: number; x: number; y: number; x0: number; y0: number; moved: boolean; button: number } | null = null
    let space = false
    let hover = false
    /** The last middle click (press + release without a pan), for the double middle-click. */
    let lastMiddle = { t: -1e9, x: 0, y: 0 }
    let middleDragged = false
    let swallowClick = 0
    const mark = () => {
      if (pan) surface.dataset.pan = 'active'
      else if (space && hover) surface.dataset.pan = 'ready'
      else delete surface.dataset.pan
    }
    const end = (e: PointerEvent) => {
      if (!pan || e.pointerId !== pan.id) return
      if (pan.button === 1) middleDragged = pan.moved
      if (pan.button === 0) swallowClick = performance.now()
      if (surface.hasPointerCapture(e.pointerId)) surface.releasePointerCapture(e.pointerId)
      pan = null
      mark()
    }
    const onDown = (e: PointerEvent) => {
      const middle = e.button === 1
      if (!middle && !(e.button === 0 && space)) return
      e.preventDefault() // no autoscroll / text selection / focus change
      e.stopPropagation()
      if (pan) return
      stopViewAnimation()
      pan = { id: e.pointerId, x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY, moved: false, button: e.button }
      surface.setPointerCapture(e.pointerId)
      mark()
    }
    const onMove = (e: PointerEvent) => {
      if (!pan || e.pointerId !== pan.id) return
      e.stopPropagation()
      const dx = e.clientX - pan.x
      const dy = e.clientY - pan.y
      if (!dx && !dy) return
      pan.x = e.clientX
      pan.y = e.clientY
      if (!pan.moved && Math.hypot(e.clientX - pan.x0, e.clientY - pan.y0) > 3) pan.moved = true
      panView(dx, dy)
    }
    const onMouseDown = (e: MouseEvent) => {
      if (e.button === 1) e.preventDefault() // Windows autoscroll compass
    }
    // Double middle-click = Fit: the OS click count (detail) when the browser reports it, else a 500 ms window.
    const onAuxClick = (e: MouseEvent) => {
      if (e.button !== 1) return
      e.preventDefault() // no paste / open-in-new-tab
      e.stopPropagation()
      if (middleDragged) return void (lastMiddle.t = -1e9)
      const now = performance.now()
      const second = now - lastMiddle.t < 500 && Math.hypot(e.clientX - lastMiddle.x, e.clientY - lastMiddle.y) < 8
      if (e.detail === 2 || second) {
        lastMiddle.t = -1e9
        resetView()
      } else lastMiddle = { t: now, x: e.clientX, y: e.clientY }
    }
    const onClick = (e: MouseEvent) => {
      if (performance.now() - swallowClick < 300) {
        e.preventDefault()
        e.stopPropagation()
        swallowClick = 0
      }
    }
    const onEnter = () => {
      hover = true
      mark()
    }
    const onLeave = () => {
      hover = false
      mark()
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.code !== 'Space' || e.ctrlKey || e.metaKey || e.altKey) return
      if (!hover || isTypingTarget(e.target) || document.querySelector('[data-dialog]')) return
      e.preventDefault() // no page scroll / button activation while Space is held over the stage
      if (space) return
      space = true
      mark()
    }
    const onKeyUp = (e: KeyboardEvent) => {
      if (e.code !== 'Space' || !space) return
      space = false
      mark()
    }
    const onBlur = () => {
      space = false
      mark()
    }
    surface.addEventListener('pointerdown', onDown, true)
    surface.addEventListener('pointermove', onMove, true)
    surface.addEventListener('pointerup', end, true)
    surface.addEventListener('pointercancel', end, true)
    surface.addEventListener('lostpointercapture', end)
    surface.addEventListener('mousedown', onMouseDown, true)
    surface.addEventListener('auxclick', onAuxClick, true)
    surface.addEventListener('click', onClick, true)
    surface.addEventListener('pointerenter', onEnter)
    surface.addEventListener('pointerleave', onLeave)
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('keyup', onKeyUp)
    window.addEventListener('blur', onBlur)
    hover = surface.matches(':hover')
    return () => {
      surface.removeEventListener('pointerdown', onDown, true)
      surface.removeEventListener('pointermove', onMove, true)
      surface.removeEventListener('pointerup', end, true)
      surface.removeEventListener('pointercancel', end, true)
      surface.removeEventListener('lostpointercapture', end)
      surface.removeEventListener('mousedown', onMouseDown, true)
      surface.removeEventListener('auxclick', onAuxClick, true)
      surface.removeEventListener('click', onClick, true)
      surface.removeEventListener('pointerenter', onEnter)
      surface.removeEventListener('pointerleave', onLeave)
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('keyup', onKeyUp)
      window.removeEventListener('blur', onBlur)
      delete surface.dataset.pan
    }
  }, [surface])
}

/** Cursor while Space is held over the stage / while panning; it beats the canvas's own hover cursors. */
export const PAN_CURSOR_CSS =
  '[data-stage-surface][data-pan="ready"],[data-stage-surface][data-pan="ready"] *{cursor:grab!important}' +
  '[data-stage-surface][data-pan="active"],[data-stage-surface][data-pan="active"] *{cursor:grabbing!important}'

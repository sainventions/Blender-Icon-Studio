// CAD-style stage navigation (pure, unit-tested in web/src/features/editor/stage/view.test.mjs).
//
// The stage view is a VIEW transform — like a CAD viewport it never changes the project's render framing
// (camera.zoom): the icon frame (the square the Blender camera renders) is placed on the stage area at
// `fit × zoom` CSS px, its centre offset from the fitted position by (x, y) × fit. The Live (three.js), Render and
// Compare views all place the frame from the same view, so switching views keeps the region you were looking at.
// `null` = Fit: every view fits the whole icon (Render / Compare never above the render's own pixels, frame.ts).
import { frameSide } from './frame'

export interface StageView {
  /** Frame side / fitted side (1 = 100 %). */
  zoom: number
  /** Frame-centre offset from the fitted centre, in units of the fitted side (resize-proof). */
  x: number
  y: number
}

export interface StageLayout {
  /** Stage area (= the live canvas), CSS px. */
  width: number
  height: number
  dpr: number
  /** Fitted frame side at 100 % (room for the floating toolbar above, a margin around). */
  fit: number
  /** This view's Fit side: `fit`, or for a shown render at most its own pixels (frame.ts). */
  base: number
  /** Frame centre at Fit. */
  homeX: number
  homeY: number
}

/** The frame on the stage area: top-left + side, CSS px (whole device pixels relative to the area). */
export interface FrameRect {
  x: number
  y: number
  side: number
}

export const MIN_ZOOM = 0.25
export const MAX_ZOOM = 8
/** Room above the frame for the floating toolbar, and the margin elsewhere (CSS px). */
export const FRAME_PAD_TOP = 64
export const FRAME_PAD = 40
/** Panning keeps at least this much of the frame on screen (CSS px). */
const KEEP_VISIBLE = 64

export const clampZoom = (z: number): number => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Number.isFinite(z) ? z : 1))

export function stageLayout(width: number, height: number, dpr = 1, nativePx: number | null = null): StageLayout {
  const fit = Math.max(160, Math.min(width - FRAME_PAD * 2, height - FRAME_PAD_TOP - FRAME_PAD))
  return {
    width,
    height,
    dpr,
    fit,
    base: frameSide(fit, 1, dpr, nativePx),
    homeX: width / 2,
    // centred in the area below the toolbar's padding (FRAME_PAD_TOP − FRAME_PAD extra at the top)
    homeY: (height + FRAME_PAD_TOP - FRAME_PAD) / 2,
  }
}

/** The explicit view equivalent to `view` (Fit → this view's fitted zoom, centred). */
export function viewOf(view: StageView | null, L: StageLayout): StageView {
  return view ?? { zoom: L.base / L.fit, x: 0, y: 0 }
}

const snap = (v: number, dpr: number) => Math.round(v * dpr) / dpr

/** Where the frame sits on the stage area. Snapped to whole device pixels (a 1:1 render is not resampled). */
export function frameRect(view: StageView | null, L: StageLayout): FrameRect {
  const side = view ? Math.max(1, Math.round(L.fit * view.zoom * L.dpr)) / L.dpr : L.base
  const v = viewOf(view, L)
  const cx = L.homeX + v.x * L.fit
  const cy = L.homeY + v.y * L.fit
  return { x: snap(cx - side / 2, L.dpr), y: snap(cy - side / 2, L.dpr), side }
}

/** Zoom percentage shown in the toolbar (relative to the fitted 100 % frame). */
export function zoomPercent(view: StageView | null, L: StageLayout): number {
  return Math.round((viewOf(view, L).zoom || 1) * 100)
}

/** Keep at least KEEP_VISIBLE px of the frame inside the area (the icon can never be panned out of reach). */
export function clampPan(view: StageView, L: StageLayout): StageView {
  const side = L.fit * view.zoom
  const keep = Math.min(KEEP_VISIBLE, side / 2)
  const cx = L.homeX + view.x * L.fit
  const cy = L.homeY + view.y * L.fit
  const nx = Math.min(L.width - keep + side / 2, Math.max(keep - side / 2, cx))
  const ny = Math.min(L.height - keep + side / 2, Math.max(keep - side / 2, cy))
  if (nx === cx && ny === cy) return view
  return { zoom: view.zoom, x: (nx - L.homeX) / L.fit, y: (ny - L.homeY) / L.fit }
}

/**
 * Zoom by `factor` about the area point (px, py): the art under that point stays put. The zoom is clamped to
 * MIN_ZOOM..MAX_ZOOM (the effective factor shrinks accordingly).
 */
export function zoomAt(view: StageView | null, factor: number, px: number, py: number, L: StageLayout): StageView {
  const v = viewOf(view, L)
  const zoom = clampZoom(v.zoom * factor)
  const f = zoom / v.zoom
  const cx = L.homeX + v.x * L.fit
  const cy = L.homeY + v.y * L.fit
  const nx = px - (px - cx) * f
  const ny = py - (py - cy) * f
  return clampPan({ zoom, x: (nx - L.homeX) / L.fit, y: (ny - L.homeY) / L.fit }, L)
}

/** Pan by (dx, dy) CSS px (the frame follows the pointer). */
export function panBy(view: StageView | null, dx: number, dy: number, L: StageLayout): StageView {
  const v = viewOf(view, L)
  return clampPan({ zoom: v.zoom, x: v.x + dx / L.fit, y: v.y + dy / L.fit }, L)
}

/** Zoom to `zoom` about the area centre (toolbar buttons / menu: the region in the middle of the stage stays). */
export function zoomTo(view: StageView | null, zoom: number, L: StageLayout): StageView {
  const v = viewOf(view, L)
  return zoomAt(v, clampZoom(zoom) / v.zoom, L.width / 2, L.homeY, L)
}

/**
 * Multiplicative zoom for one wheel event: plain wheel ≈ ×1.22 per mouse notch (100 px), trackpad pinch (the OS
 * sends ctrl+wheel with small pixel deltas) proportionally faster per pixel. Lines / pages are converted to px.
 */
export function wheelZoomFactor(e: { deltaY: number; deltaMode?: number; ctrlKey?: boolean }, pagePx = 800): number {
  const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? pagePx : 1
  const d = Math.max(-240, Math.min(240, e.deltaY * unit))
  return Math.exp(-d * (e.ctrlKey ? 0.01 : 0.002))
}

/** A coarse (mouse-notch-like) wheel step that is worth easing; trackpads send many small pixel deltas. */
export function isNotchWheel(e: { deltaY: number; deltaMode?: number; ctrlKey?: boolean }): boolean {
  return !e.ctrlKey && (e.deltaMode !== 0 || Math.abs(e.deltaY) >= 50)
}

/** Interpolates two views (zoom geometric, centre linear) — the eased Fit / preset transitions. */
export function lerpView(a: StageView, b: StageView, t: number): StageView {
  return {
    zoom: a.zoom * Math.pow(b.zoom / a.zoom, t),
    x: a.x + (b.x - a.x) * t,
    y: a.y + (b.y - a.y) * t,
  }
}

export function sameView(a: StageView | null, b: StageView | null): boolean {
  if (!a || !b) return a === b
  return Math.abs(a.zoom - b.zoom) < 1e-9 && Math.abs(a.x - b.x) < 1e-9 && Math.abs(a.y - b.y) < 1e-9
}

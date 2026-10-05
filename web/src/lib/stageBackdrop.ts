// The editor stage's backdrop, defined once: the CSS of the stage element and of the Render view's checkerboard is
// generated from these numbers, and the live three.js viewport re-creates the very same pixels in a shader
// (viewport/scene/Backdrop.tsx), so the live canvas fades into the stage without a visible box.
import type { CSSProperties } from 'react'

export type Rgb8 = readonly [number, number, number]

export interface StageBackdropSpec {
  /** Stage background colour (sRGB 0..255). */
  base: Rgb8
  /** Soft accent glow: CSS `radial-gradient(ellipse rx ry at cx cy, rgb / alpha, transparent stop)` over the stage box. */
  glow: { rgb: Rgb8; alpha: number; cx: number; cy: number; rx: number; ry: number; stop: number }
  /** Faint dot grid: one dot per `spacing` px tile (centred), `alpha` inside `inner` px, fading out at `outer` px. */
  dots: { spacing: number; alpha: number; inner: number; outer: number }
  /**
   * "Transparent" checkerboard of the icon frame (Live and Render views): `cell` px squares of `a` / `b`, shown at
   * `opacity` in the centre and faded out radially (closest side) so it melts into the stage.
   */
  checker: { a: Rgb8; b: Rgb8; cell: number; opacity: number }
}

export const STAGE_BACKDROP: StageBackdropSpec = {
  base: [11, 11, 15],
  glow: { rgb: [143, 125, 255], alpha: 0.07, cx: 0.5, cy: 0.42, rx: 0.8, ry: 0.7, stop: 0.7 },
  dots: { spacing: 22, alpha: 0.035, inner: 1, outer: 1.2 },
  checker: { a: [27, 27, 33], b: [36, 36, 44], cell: 6, opacity: 0.4 },
}

const rgb = (c: Rgb8) => `rgb(${c[0]} ${c[1]} ${c[2]})`
const pct = (v: number) => `${+(v * 100).toFixed(3)}%`

/** Background of the stage element (the area around the icon frame). */
export function stageBackgroundStyle(spec: StageBackdropSpec = STAGE_BACKDROP): CSSProperties {
  const { glow: g, dots: d } = spec
  return {
    backgroundColor: rgb(spec.base),
    backgroundImage:
      `radial-gradient(ellipse ${pct(g.rx)} ${pct(g.ry)} at ${pct(g.cx)} ${pct(g.cy)}, ` +
      `rgb(${g.rgb[0]} ${g.rgb[1]} ${g.rgb[2]} / ${g.alpha}), transparent ${pct(g.stop)}), ` +
      `radial-gradient(circle at 50% 50%, rgb(255 255 255 / ${d.alpha}) ${d.inner}px, transparent ${d.outer}px)`,
    backgroundSize: `100% 100%, ${d.spacing}px ${d.spacing}px`,
  }
}

/**
 * The faded checkerboard drawn behind a render in the icon frame (absolutely positioned over the frame box). The
 * cell at the frame's top-left is `b`, then they alternate; the live viewport's shader uses the same phase.
 */
export function checkerOverlayStyle(spec: StageBackdropSpec = STAGE_BACKDROP): CSSProperties {
  const c = spec.checker
  const mask = 'radial-gradient(closest-side, black, transparent)'
  return {
    backgroundImage: `repeating-conic-gradient(${rgb(c.a)} 0 25%, ${rgb(c.b)} 0 50%)`,
    backgroundSize: `${c.cell * 2}px ${c.cell * 2}px`,
    opacity: c.opacity,
    maskImage: mask,
    WebkitMaskImage: mask,
  }
}

// ------------------------------------------------------------------------------------------ pixel model (JS mirror)
// The same composite the browser paints, in sRGB 0..1 (CSS mixes colours gamma-encoded). Used by the viewport's
// fake-glass "behind" colour and by tests; the viewport shader (Backdrop.tsx) is a line-by-line twin.

/** Stage colour at (x, y) CSS px from the stage box's top-left (box w × h). */
export function stageColorAt(x: number, y: number, w: number, h: number, spec: StageBackdropSpec = STAGE_BACKDROP): [number, number, number] {
  const { dots: d, glow: g } = spec
  let col = spec.base.map((v) => v / 255) as [number, number, number]
  const mx = (((x % d.spacing) + d.spacing) % d.spacing) - d.spacing / 2
  const my = (((y % d.spacing) + d.spacing) % d.spacing) - d.spacing / 2
  const aDot = d.alpha * clamp01((d.outer - Math.hypot(mx, my)) / (d.outer - d.inner))
  col = col.map((v) => v + (1 - v) * aDot) as [number, number, number]
  const t = Math.hypot((x - g.cx * w) / (g.rx * w), (y - g.cy * h) / (g.ry * h))
  const aGlow = g.alpha * clamp01(1 - t / g.stop)
  return col.map((v, i) => v + (g.rgb[i] / 255 - v) * aGlow) as [number, number, number]
}

/** Checkerboard over `under` at (x, y) CSS px from the frame's top-left (frame w × h). */
export function checkerOver(under: [number, number, number], x: number, y: number, w: number, h: number, spec: StageBackdropSpec = STAGE_BACKDROP): [number, number, number] {
  const c = spec.checker
  const odd = (Math.floor(x / c.cell) + Math.floor(y / c.cell)) % 2 !== 0
  const sq = odd ? c.a : c.b
  const a = c.opacity * clamp01(1 - Math.hypot((x - w / 2) / (w / 2), (y - h / 2) / (h / 2)))
  return under.map((v, i) => v + (sq[i] / 255 - v) * a) as [number, number, number]
}

function clamp01(v: number): number {
  return v < 0 ? 0 : v > 1 ? 1 : v
}

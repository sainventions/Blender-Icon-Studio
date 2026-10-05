// Icon-frame sizing for the stage (pure, unit-tested in web/src/viewport/tests/ui-polish.test.mjs).

/**
 * Side of the icon frame in CSS px. Live view (`nativePx` null): the fitted square × zoom. Render / Compare with a
 * render: at 100 % ("Fit") never larger than the render's own pixels (a 512 px preview is shown at 512 device px,
 * centred, instead of being blown up to the fitted ~740 px), and zoom scales from there (zooming past 1:1 is still
 * possible, explicitly). Snapped to whole device pixels so a 1:1 render is not resampled.
 */
export function frameSide(fit: number, zoom: number, dpr: number, nativePx: number | null | undefined): number {
  const base = nativePx && nativePx > 0 ? Math.min(fit, nativePx / dpr) : fit
  return Math.max(1, Math.round(base * zoom * dpr)) / dpr
}

/** Zoom at which a render `nativePx` wide shows one image pixel per screen pixel (1 when Fit already is 1:1). */
export function actualPixelsZoom(fit: number, dpr: number, nativePx: number): number {
  const css = nativePx / dpr
  return css / Math.min(fit, css)
}

/** One image pixel per device pixel (`scale` = device px per image px; null = unknown). */
export function isOneToOne(scale: number | null | undefined): boolean {
  return scale != null && Math.abs(scale - 1) < 0.005
}

/** "1:1" when one image pixel is one screen pixel, else the display scale ("72%", or "150%" past native). */
export function pixelScaleLabel(scale: number): string {
  return isOneToOne(scale) ? '1:1' : `${Math.round(scale * 100)}%`
}

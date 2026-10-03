// Worker scene.soft_alpha_fraction (raster glow halos), kept free of DOM-only imports so it can be unit tested.

/** Worker HALO_SOFT_MIN: a raster region whose image is softer than this also gets a flat halo card under it. */
export const HALO_SOFT_MIN = 0.25

/**
 * Worker soft_alpha_fraction on raw RGBA bytes: share of the visible pixels (alpha > 0.02) that are only partly opaque
 * (alpha < 0.6) around a solid core — the glow halo an alpha-traced extrusion drops; 0 when fewer than 10 % of the
 * visible pixels are solid (a translucent shading overlay).
 */
export function softAlphaFromRgba(data: ArrayLike<number>): number {
  let vis = 0
  let soft = 0
  let solid = 0
  for (let i = 3; i < data.length; i += 4) {
    const a = data[i] / 255
    if (a <= 0.02) continue
    vis++
    if (a < 0.6) soft++
    else solid++
  }
  if (!vis || solid / vis < 0.1) return 0
  return soft / vis
}

/** softAlphaFromRgba of an image at full resolution (longest side ≤ 1024), like the worker. 0 on failure. */
export function softAlphaFraction(image: CanvasImageSource & { width: number; height: number }): number {
  try {
    const w0 = Math.max(1, image.width)
    const h0 = Math.max(1, image.height)
    const k = Math.min(1, 1024 / Math.max(w0, h0))
    const w = Math.max(1, Math.round(w0 * k))
    const h = Math.max(1, Math.round(h0 * k))
    const canvas = document.createElement('canvas')
    canvas.width = w
    canvas.height = h
    const ctx = canvas.getContext('2d', { willReadFrequently: true })
    if (!ctx) return 0
    ctx.drawImage(image, 0, 0, w, h)
    return softAlphaFromRgba(ctx.getImageData(0, 0, w, h).data)
  } catch {
    return 0
  }
}

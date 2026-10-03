// Which finished render the Render / Compare views show for an appearance (store/render.ts `latest`).
import type { Quality } from '../types'

const QUALITY_RANK: Record<Quality, number> = { draft: 0, preview: 1, final: 2, ultra: 3 }

export interface PickableRender {
  quality: Quality
  state: string
  url?: string
  width?: number
  sig?: string
}

/**
 * True when a newly finished renditions-strip render (`purpose` 'rendition', 256 px) must NOT replace the render shown
 * for its appearance: the shown one renders the very same state (signature), is larger and at least as good a tier.
 * Fit shows a render at most 1:1, so the 256 px rendition would shrink the frame to a quarter of the area (to 171 CSS
 * px at 150 % display scaling) although a sharper image of that state is already there. A rendition of a newer state,
 * or of a better tier (a 256 px Cycles preview after a 512 px EEVEE draft), still replaces it.
 */
export function keepsSharperRender(shown: PickableRender | undefined, incoming: PickableRender & { purpose: string }): boolean {
  if (!shown || incoming.purpose !== 'rendition') return false
  if (shown.state !== 'done' || !shown.url || !shown.sig || shown.sig !== incoming.sig) return false
  return (shown.width ?? 0) > (incoming.width ?? 0) && QUALITY_RANK[shown.quality] >= QUALITY_RANK[incoming.quality]
}

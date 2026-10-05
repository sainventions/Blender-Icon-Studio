// User-facing names for appearances and render engines, in one place, so every surface (top bar, renditions strip,
// matrix, waterfall, tooltips, status bar, toasts) says the same thing.
import type { AppearanceId, Presets, SourceInfo } from '../types'

const LONG: Record<AppearanceId, string> = {
  light: 'Default',
  dark: 'Dark',
  'clear-light': 'Clear Light',
  'clear-dark': 'Clear Dark',
  'tinted-light': 'Tinted Light',
  'tinted-dark': 'Tinted Dark',
}

const SHORT: Record<AppearanceId, string> = {
  light: 'Default',
  dark: 'Dark',
  'clear-light': 'Clear',
  'clear-dark': 'Clear D',
  'tinted-light': 'Tinted',
  'tinted-dark': 'Tinted D',
}

/**
 * Display name of an appearance. The base appearance is always Apple's "Default" (Icon Composer), never "Light";
 * presets.json's `short` for it still says "Light", so it is not consulted for that one. `short` = for tight spots
 * (top-bar segments, renditions strip); pair it with the long name in a tooltip.
 */
export function appearanceLabel(id: AppearanceId | string, presets?: Presets | null, form: 'long' | 'short' = 'long'): string {
  const known = id in LONG ? (id as AppearanceId) : null
  if (!known) return String(id)
  if (known === 'light') return LONG.light
  const p = presets?.appearances?.[known]
  return (form === 'short' ? p?.short : p?.label) || (form === 'short' ? SHORT : LONG)[known]
}

/** "Cycles" / "EEVEE" from whatever the server or worker reports ('cycles', 'CYCLES', 'BLENDER_EEVEE_NEXT', …). */
export function engineLabel(engine: string | null | undefined, quality?: string | null): string {
  const e = (engine ?? '').toLowerCase()
  if (e.includes('cycles')) return 'Cycles'
  if (e.includes('eevee')) return 'EEVEE'
  if (e.includes('workbench')) return 'Workbench'
  if (e) return e[0].toUpperCase() + e.slice(1)
  return quality === 'draft' ? 'EEVEE' : 'Cycles'
}

export type SourcePlateKind = 'full-bleed' | 'plate' | 'none'

/**
 * What the importer found behind the art (SourcePlateBadge). Full-bleed wins: the artwork itself is the icon shape,
 * so "No plate" would be misleading.
 */
export function sourcePlateKind(source: Pick<SourceInfo, 'plateDetected' | 'fullBleed'>): SourcePlateKind {
  return source.fullBleed ? 'full-bleed' : source.plateDetected ? 'plate' : 'none'
}

// Render ▸ Look ▸ Colour: the colour modes of shared/presets.json `colorModes` (labels from there), the worker's default
// marked as such. blender_worker/presets.DEFAULT_COLOR_MODE ('brand' since round 5) is what a project without (or with
// an unknown) render.colorMode renders with; the live view resolves it the same way (displayTransform.colorModeId).
import type { Presets } from '../types'
import { DEFAULT_COLOR_MODE } from '../viewport/scene/displayTransform'

export interface ColorModeOption {
  value: string
  label: string
  description: string
}

/** Select options for the colour modes in presets order; the worker's default mode is labelled "(default)". */
export function colorModeOptions(presets: Pick<Presets, 'colorModes'>): ColorModeOption[] {
  return Object.entries(presets.colorModes ?? {}).map(([id, m]) => ({
    value: id,
    label: id === DEFAULT_COLOR_MODE ? `${m.label} (default)` : m.label,
    description: `${m.viewTransform}${m.softClip ? ' + highlight soft clip' : ''}${m.look && m.look !== 'None' ? ` · ${m.look}` : ''}`,
  }))
}

/** The mode a project renders with: its own when the presets know it, else the default (worker color_mode_id). */
export function effectiveColorMode(mode: string | null | undefined, presets: Pick<Presets, 'colorModes'>): string {
  return mode && presets.colorModes && mode in presets.colorModes ? mode : DEFAULT_COLOR_MODE
}

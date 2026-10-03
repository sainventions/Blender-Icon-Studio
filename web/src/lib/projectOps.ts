// Pure, structurally-sharing helpers for editing a Project immutably.
import type {
  AppearanceId,
  AppearanceOverride,
  Fill,
  Layer,
  LayerOverride,
  MaterialPreset,
  MaterialSpec,
  Project,
  SvgElement,
} from '../types'
import { hashString } from './format'
import { linearPointsFromAngle } from './color'

export const APPEARANCE_IDS: AppearanceId[] = [
  'light',
  'dark',
  'clear-light',
  'clear-dark',
  'tinted-light',
  'tinted-dark',
]

// ------------------------------------------------------------------------------------------ layers
export function updateLayer(p: Project, id: string, fn: (l: Layer) => Layer): Project {
  let changed = false
  const layers = p.layers.map((l) => {
    if (l.id !== id) return l
    const n = fn(l)
    if (n !== l) changed = true
    return n
  })
  return changed ? { ...p, layers } : p
}

export function updateLayers(p: Project, ids: readonly string[], fn: (l: Layer) => Layer): Project {
  if (!ids.length) return p
  const set = new Set(ids)
  let changed = false
  const layers = p.layers.map((l) => {
    if (!set.has(l.id)) return l
    const n = fn(l)
    if (n !== l) changed = true
    return n
  })
  return changed ? { ...p, layers } : p
}

/** Reorder layers (bottom→top array) and re-assign the existing set of z values in the new order so the
 *  depth stack follows the visual order. */
export function reorderLayers(p: Project, fromIndex: number, toIndex: number): Project {
  if (fromIndex === toIndex) return p
  const layers = [...p.layers]
  const [moved] = layers.splice(fromIndex, 1)
  layers.splice(toIndex, 0, moved)
  const zs = p.layers.map((l) => l.depth.z).sort((a, b) => a - b)
  return {
    ...p,
    layers: layers.map((l, i) => (l.depth.z === zs[i] ? l : { ...l, depth: { ...l.depth, z: zs[i] } })),
  }
}

export function removeLayers(p: Project, ids: readonly string[]): Project {
  const set = new Set(ids)
  const strip = (o: AppearanceOverride): AppearanceOverride => {
    const layers = Object.fromEntries(Object.entries(o.layers ?? {}).filter(([k]) => !set.has(k)))
    return { ...o, layers }
  }
  return {
    ...p,
    layers: p.layers.filter((l) => !set.has(l.id)),
    appearances: { ...p.appearances, dark: strip(p.appearances.dark), mono: strip(p.appearances.mono) },
  }
}

// ------------------------------------------------------------------------------------------ signatures
/** Signature of everything that changes the geometry bundle (2D curves + textures). */
export function structuralSig(p: Project | null): string {
  if (!p) return ''
  return hashString(p.id + '|' + p.layers.map((l) => `${l.id}:${l.mode}:${l.elementIds.join(',')}`).join('|'))
}

/** Signature of everything that changes a render (excludes name, timestamps and the edited appearance). */
export function renderSig(p: Project): string {
  return hashString(
    JSON.stringify([p.layers, p.canvas, p.lighting, p.camera, p.appearances, p.render.colorMode, p.render.backdrop, p.render.backdropColor]),
  )
}

// ------------------------------------------------------------------------------------------ appearances
export type OverrideBucket = 'dark' | 'mono'

/** Which designer-annotated override set an appearance edits (light = base project). */
export function overrideBucket(a: AppearanceId): OverrideBucket | null {
  if (a === 'light') return null
  if (a === 'dark') return 'dark'
  return 'mono'
}

export const BUCKET_LABEL: Record<OverrideBucket, string> = { dark: 'Dark', mono: 'Mono' }

export type OverrideField = keyof LayerOverride

export function getLayerOverride(p: Project, bucket: OverrideBucket, layerId: string): LayerOverride | undefined {
  return p.appearances[bucket]?.layers?.[layerId]
}

export function setLayerOverride(
  p: Project,
  bucket: OverrideBucket,
  layerIds: readonly string[],
  patch: Partial<LayerOverride>,
): Project {
  const cur = p.appearances[bucket]
  const layers = { ...(cur.layers ?? {}) }
  for (const id of layerIds) {
    const merged: LayerOverride = { ...(layers[id] ?? {}), ...patch }
    for (const k of Object.keys(merged) as OverrideField[]) if (merged[k] == null) delete merged[k]
    if (Object.keys(merged).length) layers[id] = merged
    else delete layers[id]
  }
  return { ...p, appearances: { ...p.appearances, [bucket]: { ...cur, layers } } }
}

export function clearLayerOverride(p: Project, bucket: OverrideBucket, layerIds: readonly string[], field: OverrideField) {
  return setLayerOverride(p, bucket, layerIds, { [field]: null } as Partial<LayerOverride>)
}

export function setPlateFillOverride(p: Project, bucket: OverrideBucket, fill: Fill | null): Project {
  return { ...p, appearances: { ...p.appearances, [bucket]: { ...p.appearances[bucket], plateFill: fill } } }
}

/** Layer with the designer overrides of the given appearance applied (inspector display; the full visual
 *  transform lives in lib/appearance.ts). */
export function effectiveLayer(p: Project, appearance: AppearanceId, layer: Layer): Layer {
  const bucket = overrideBucket(appearance)
  if (!bucket) return layer
  const o = getLayerOverride(p, bucket, layer.id)
  if (!o) return layer
  return {
    ...layer,
    fill: o.fill ?? layer.fill,
    opacity: o.opacity ?? layer.opacity,
    visible: o.visible ?? layer.visible,
    blendMode: o.blendMode ?? layer.blendMode,
    material: o.material ?? layer.material,
  }
}

export function effectivePlateFill(p: Project, appearance: AppearanceId): Fill {
  const bucket = overrideBucket(appearance)
  return (bucket && p.appearances[bucket]?.plateFill) || p.canvas.plate.fill
}

/** All overrides of a layer field across buckets (for nested override rows). */
export function overridesOf(p: Project, layerId: string, field: OverrideField): { bucket: OverrideBucket; value: unknown }[] {
  const out: { bucket: OverrideBucket; value: unknown }[] = []
  for (const bucket of ['dark', 'mono'] as const) {
    const v = getLayerOverride(p, bucket, layerId)?.[field]
    if (v != null) out.push({ bucket, value: v })
  }
  return out
}

// ------------------------------------------------------------------------------------------ materials
export function paramValue(preset: MaterialPreset | undefined, spec: MaterialSpec, key: string): number | string | boolean {
  const v = spec.params?.[key]
  if (v !== undefined) return v
  return preset?.params[key]?.default ?? 0
}

export function isParamModified(preset: MaterialPreset | undefined, spec: MaterialSpec, key: string): boolean {
  const v = spec.params?.[key]
  return v !== undefined && v !== preset?.params[key]?.default
}

export function withParam(spec: MaterialSpec, key: string, value: number | string | boolean | undefined): MaterialSpec {
  const params = { ...(spec.params ?? {}) }
  if (value === undefined) delete params[key]
  else params[key] = value
  return { ...spec, params }
}

// ------------------------------------------------------------------------------------------ fills
export type FillType = Fill['type']

/** Build a sensible fill of a given type, carrying colours over from the current fill where possible. */
export function convertFill(type: FillType, current: Fill | null | undefined, fallbackColor = '#7c6cff'): Fill {
  const baseColor =
    current?.type === 'solid'
      ? current.color
      : current?.type === 'linear' || current?.type === 'radial'
        ? current.stops[0]?.color ?? fallbackColor
        : fallbackColor
  const stops =
    current?.type === 'linear' || current?.type === 'radial'
      ? current.stops
      : [
          { offset: 0, color: baseColor, opacity: 1 },
          { offset: 1, color: shade(baseColor), opacity: 1 },
        ]
  switch (type) {
    case 'auto':
      return { type: 'auto' }
    case 'none':
      return { type: 'none' }
    case 'solid':
      return { type: 'solid', color: baseColor, opacity: current?.type === 'solid' ? current.opacity : 1 }
    case 'linear': {
      const pts = linearPointsFromAngle(180)
      return { type: 'linear', stops, ...(current?.type === 'linear' ? { start: current.start, end: current.end } : pts) }
    }
    case 'radial':
      return { type: 'radial', stops, center: [0, 0], radius: 1 }
    case 'system-light':
      return { type: 'system-light' }
    case 'system-dark':
      return { type: 'system-dark' }
  }
}

function shade(hex: string): string {
  const n = parseInt(hex.slice(1), 16)
  const r = Math.max(0, ((n >> 16) & 255) * 0.62)
  const g = Math.max(0, ((n >> 8) & 255) * 0.62)
  const b = Math.max(0, (n & 255) * 0.62)
  return '#' + [r, g, b].map((v) => Math.round(v).toString(16).padStart(2, '0')).join('')
}

// ------------------------------------------------------------------------------------------ misc
export function elementMap(p: Project): Map<string, SvgElement> {
  return new Map(p.elements.map((e) => [e.id, e]))
}

export function layerOfElement(p: Project, elementId: string): Layer | undefined {
  return p.layers.find((l) => l.elementIds.includes(elementId))
}

export function uniqueLayerName(p: Project, base: string): string {
  const names = new Set(p.layers.map((l) => l.name))
  if (!names.has(base)) return base
  for (let i = 2; i < 999; i++) if (!names.has(`${base} ${i}`)) return `${base} ${i}`
  return `${base} ${Date.now()}`
}

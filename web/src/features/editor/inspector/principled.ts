// The material model the inspector edits (PLAN §11): every shape renders with ONE Principled BSDF whose inputs are the
// Principled schema of shared/presets.json (params grouped like Blender's node panel). Presets are only starting values;
// a shape may carry its own material in Layer.elementMaterials (merged over the layer's like blender_worker
// presets.resolve_material does).
import type { Layer, MaterialPreset, MaterialSpec, ParamSchema, Presets } from '../../../types'

/** Blender's own panel order: the fallback when presets.json carries no `principledSchema.groups`. */
export const PRINCIPLED_GROUPS = ['Paint', 'Base', 'Subsurface', 'Specular', 'Transmission', 'Coat', 'Sheen', 'Emission', 'Thin Film']

/** Groups that are always relevant (the art colour and the base inputs). */
const ALWAYS_ACTIVE = new Set(['Paint', 'Base'])

/** The weight input that switches a Principled lobe on: at 0 the group does nothing, whatever its other inputs say. */
const GROUP_GATE: Record<string, string> = {
  Subsurface: 'subsurfaceWeight',
  Transmission: 'transmission',
  Coat: 'coatWeight',
  Sheen: 'sheenWeight',
  Emission: 'emissionStrength',
  'Thin Film': 'thinFilmThickness',
}

/** A fresh Blender 5.0 Principled BSDF node's inputs (schema keys): a group whose values all sit here is "default". */
const PRINCIPLED_NEUTRAL: Record<string, number> = {
  metallic: 0,
  roughness: 0.5,
  ior: 1.5,
  alpha: 1,
  diffuseRoughness: 0,
  subsurfaceWeight: 0,
  subsurfaceScale: 0.05,
  subsurfaceAnisotropy: 0,
  specularIorLevel: 0.5,
  specularTint: 0,
  anisotropic: 0,
  anisotropicRotation: 0,
  transmission: 0,
  coatWeight: 0,
  coatRoughness: 0.03,
  coatIor: 1.5,
  coatTint: 0,
  sheenWeight: 0,
  sheenRoughness: 0.5,
  sheenTint: 0,
  emissionStrength: 0,
  thinFilmThickness: 0,
  thinFilmIor: 1.33,
}

/** Pre-processing params that only act while another input is non-zero (the worker adds their nodes only then). */
export const PARAM_NEEDS: Record<string, { key: string; hint: string }> = {
  grainScale: { key: 'grain', hint: 'Acts once Surface Grain is above 0.' },
  filmVariation: { key: 'thinFilmThickness', hint: 'Acts once Thin Film ▸ Thickness is above 0.' },
  anisotropicRotation: { key: 'anisotropic', hint: 'Acts once Anisotropic is above 0.' },
}

export type ParamValue = number | string | boolean

export interface ParamGroup {
  name: string
  params: [string, ParamSchema][]
}

/** The preset's params grouped in Blender's panel order (reserved `__*` keys and unknown groups at the end). */
export function principledGroups(presets: Presets, preset: MaterialPreset | undefined): ParamGroup[] {
  if (!preset) return []
  const order = presets.principledSchema?.groups?.length ? presets.principledSchema.groups : PRINCIPLED_GROUPS
  const by = new Map<string, [string, ParamSchema][]>()
  for (const [key, schema] of Object.entries(preset.params)) {
    if (key.startsWith('__')) continue
    const g = schema.group ?? 'Base'
    const list = by.get(g) ?? []
    list.push([key, schema])
    by.set(g, list)
  }
  const names = [...order.filter((g) => by.has(g)), ...[...by.keys()].filter((g) => !order.includes(g))]
  return names.map((name) => ({ name, params: by.get(name)! }))
}

/** Value of `key` for `spec`: its own param, else the inherited material's (same preset only), else the preset default. */
export function resolvedParam(preset: MaterialPreset | undefined, spec: MaterialSpec, key: string, inherited?: MaterialSpec | null): ParamValue {
  const own = spec.params?.[key]
  if (own !== undefined) return own
  if (inherited && inherited.preset === spec.preset) {
    const v = inherited.params?.[key]
    if (v !== undefined) return v
  }
  return preset?.params[key]?.default ?? 0
}

/** What `key` falls back to when `spec` does not set it (the inherited material, else the preset default). */
export function fallbackParam(preset: MaterialPreset | undefined, spec: MaterialSpec, key: string, inherited?: MaterialSpec | null): ParamValue {
  return resolvedParam(preset, { ...spec, params: {} }, key, inherited)
}

/** `spec` sets `key` to something other than what it would fall back to. */
export function paramOverridden(preset: MaterialPreset | undefined, spec: MaterialSpec, key: string, inherited?: MaterialSpec | null): boolean {
  const own = spec.params?.[key]
  return own !== undefined && own !== fallbackParam(preset, spec, key, inherited)
}

/** A group does something visible: Paint / Base always; a gated lobe when its weight is above 0; otherwise when any
 *  input differs from a fresh Principled node. Drives which groups start expanded. */
export function groupActive(group: ParamGroup, value: (key: string) => ParamValue): boolean {
  if (ALWAYS_ACTIVE.has(group.name)) return true
  const gate = GROUP_GATE[group.name]
  if (gate && group.params.some(([k]) => k === gate)) return Number(value(gate)) > 0
  return group.params.some(([k]) => k in PRINCIPLED_NEUTRAL && Math.abs(Number(value(k)) - PRINCIPLED_NEUTRAL[k]) > 1e-6)
}

/** The gate (weight) input of a group, if it has one. */
export function groupGate(group: ParamGroup): string | null {
  const gate = GROUP_GATE[group.name]
  return gate && group.params.some(([k]) => k === gate) ? gate : null
}

// ------------------------------------------------------------------------------------------ per-shape materials
/** The material a shape renders with (blender_worker presets.resolve_material): the layer's, with the shape's own
 *  params on top; a shape override with ANOTHER preset starts from that preset's defaults. */
export function shapeMaterial(layerMaterial: MaterialSpec, own?: MaterialSpec | null): MaterialSpec {
  if (!own) return layerMaterial
  const preset = own.preset || layerMaterial.preset
  if (preset !== layerMaterial.preset) return { preset, params: { ...(own.params ?? {}) } }
  return { preset, params: { ...(layerMaterial.params ?? {}), ...(own.params ?? {}) } }
}

/** A layer with `fn` applied to the per-shape materials of `elementIds` (undefined removes the override). */
export function updateElementMaterials(
  layer: Layer,
  elementIds: readonly string[],
  fn: (current: MaterialSpec | undefined, elementId: string) => MaterialSpec | undefined,
): Layer {
  const cur = layer.elementMaterials ?? {}
  const next: Record<string, MaterialSpec> = { ...cur }
  let changed = false
  for (const id of elementIds) {
    if (!layer.elementIds.includes(id)) continue
    const before = cur[id]
    const after = fn(before, id)
    if (after === before) continue
    changed = true
    if (after) next[id] = after
    else delete next[id]
  }
  return changed ? { ...layer, elementMaterials: next } : layer
}

/** Element ids of `layer` that carry their own material. */
export function overriddenElements(layer: Layer): string[] {
  const own = layer.elementMaterials ?? {}
  return layer.elementIds.filter((id) => own[id])
}

/** Label for a schema enum option (paintMode's `base+emission` reads better as "Both"). */
export function optionLabel(key: string, option: string): string {
  if (key === 'paintMode') return option === 'base' ? 'Base Color' : option === 'emission' ? 'Emission' : option === 'base+emission' ? 'Both' : option
  return option
    .split(/[-_+ ]/)
    .filter(Boolean)
    .map((w) => w[0].toUpperCase() + w.slice(1))
    .join(' ')
}

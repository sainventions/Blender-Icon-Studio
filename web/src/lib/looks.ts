// Looks (presets.json "looks"), transferable styles and the copied-style clipboard (PLAN §10).
import { useSyncExternalStore } from 'react'
import type { Look, MaterialSpec, Presets, Project, StyleSpec } from '../types'
import { safeStorage } from './hooks'

// ------------------------------------------------------------------------------------------ reserved params
/** `__*` material params: legacy render-time "intent flags" (pre PLAN §11 appearances), never shown or saved. */
export function isReservedParam(key: string): boolean {
  return key.startsWith('__')
}

export function stripReservedParams(spec: MaterialSpec): MaterialSpec
export function stripReservedParams(spec: MaterialSpec | null | undefined): MaterialSpec | null | undefined
export function stripReservedParams(spec: MaterialSpec | null | undefined): MaterialSpec | null | undefined {
  if (!spec?.params) return spec
  const keys = Object.keys(spec.params)
  if (!keys.some(isReservedParam)) return spec
  return { ...spec, params: Object.fromEntries(Object.entries(spec.params).filter(([k]) => !isReservedParam(k))) }
}

/** A project without any reserved `__*` params (they must never be persisted). Returns the same object when clean. */
export function sanitizeProject(p: Project): Project {
  let changed = false
  const layers = p.layers.map((l) => {
    const m = stripReservedParams(l.material)
    if (m === l.material) return l
    changed = true
    return { ...l, material: m }
  })
  const plateMat = stripReservedParams(p.canvas.plate.material)
  if (plateMat !== p.canvas.plate.material) changed = true
  const bucket = (b: Project['appearances']['dark']) => {
    let touched = false
    const ls = Object.fromEntries(
      Object.entries(b.layers ?? {}).map(([id, o]) => {
        const m = stripReservedParams(o.material)
        if (m === o.material) return [id, o]
        touched = true
        return [id, { ...o, material: m }]
      }),
    )
    if (!touched) return b
    changed = true
    return { ...b, layers: ls }
  }
  const dark = bucket(p.appearances.dark)
  const mono = bucket(p.appearances.mono)
  if (!changed) return p
  return {
    ...p,
    layers,
    canvas: plateMat === p.canvas.plate.material ? p.canvas : { ...p.canvas, plate: { ...p.canvas.plate, material: plateMat } },
    appearances: { ...p.appearances, dark, mono },
  }
}

export function sanitizeStyle(s: StyleSpec): StyleSpec {
  return {
    ...s,
    layerDefaults: s.layerDefaults ? { ...s.layerDefaults, material: stripReservedParams(s.layerDefaults.material) } : s.layerDefaults,
    layerMaterials: s.layerMaterials ? s.layerMaterials.map((m) => stripReservedParams(m)) : s.layerMaterials,
    plate: s.plate ? { ...s.plate, material: stripReservedParams(s.plate.material) } : s.plate,
  }
}

// ------------------------------------------------------------------------------------------ looks
export function lookEntries(presets: Presets | null | undefined): [string, Look][] {
  return Object.entries(presets?.looks ?? {})
}

function paramsEqual(a: MaterialSpec['params'] | undefined, b: MaterialSpec['params'] | undefined): boolean {
  const ka = Object.keys(a ?? {}).filter((k) => !isReservedParam(k))
  const kb = Object.keys(b ?? {}).filter((k) => !isReservedParam(k))
  if (ka.length !== kb.length) return false
  return ka.every((k) => (b ?? {})[k] === (a ?? {})[k])
}

export function materialsEqual(a: MaterialSpec | null | undefined, b: MaterialSpec | null | undefined): boolean {
  if (!a || !b) return false
  return a.preset === b.preset && paramsEqual(a.params, b.params)
}

/** The material a look gives the i-th layer from the bottom (StyleSpec apply rules). */
export function lookLayerMaterial(look: Look, index: number): MaterialSpec | null {
  const mats = look.style.layerMaterials
  if (mats?.length) return mats[Math.min(index, mats.length - 1)]
  return look.style.layerDefaults?.material ?? null
}

/** Material shown on a look's thumbnail (the top layer's). */
export function lookHeroMaterial(look: Look): MaterialSpec | null {
  const mats = look.style.layerMaterials
  if (mats?.length) return mats[mats.length - 1]
  return look.style.layerDefaults?.material ?? null
}

/** Heuristic: the look whose layer materials all equal the project's (ties → plate material decides). */
export function activeLookId(
  layers: readonly { material: MaterialSpec }[] | null | undefined,
  plateMaterial: MaterialSpec | null | undefined,
  looks: Record<string, Look> | null | undefined,
): string | null {
  if (!layers?.length || !looks) return null
  const matches = Object.entries(looks).filter(([, look]) => layers.every((l, i) => materialsEqual(l.material, lookLayerMaterial(look, i))))
  if (!matches.length) return null
  const plate = matches.find(([, look]) => look.style.plate?.material?.preset === plateMaterial?.preset)
  return (plate ?? matches[0])[0]
}

// ------------------------------------------------------------------------------------------ copied style
export interface CopiedStyle {
  style: StyleSpec
  sourceName: string
  sourceId: string
  copiedAt: number
}

const COPIED_KEY = 'bis.copiedStyle'
const COPIED_EVENT = 'bis:copied-style'

let cachedRaw: string | null | undefined
let cachedValue: CopiedStyle | null = null

export function readCopiedStyle(): CopiedStyle | null {
  const raw = safeStorage.get(COPIED_KEY)
  if (raw === cachedRaw) return cachedValue
  cachedRaw = raw
  cachedValue = null
  if (raw) {
    try {
      const v = JSON.parse(raw) as CopiedStyle
      if (v && typeof v === 'object' && v.style && typeof v.style === 'object') cachedValue = v
    } catch {
      /* ignore corrupt clipboard */
    }
  }
  return cachedValue
}

export function writeCopiedStyle(c: CopiedStyle) {
  safeStorage.set(COPIED_KEY, JSON.stringify(c))
  window.dispatchEvent(new Event(COPIED_EVENT))
}

function subscribeCopied(cb: () => void) {
  const onStorage = (e: StorageEvent) => {
    if (e.key === COPIED_KEY || e.key === null) cb()
  }
  window.addEventListener('storage', onStorage)
  window.addEventListener(COPIED_EVENT, cb)
  return () => {
    window.removeEventListener('storage', onStorage)
    window.removeEventListener(COPIED_EVENT, cb)
  }
}

/** The style on the in-app clipboard (shared across tabs via localStorage). */
export function useCopiedStyle(): CopiedStyle | null {
  return useSyncExternalStore(subscribeCopied, readCopiedStyle, () => null)
}

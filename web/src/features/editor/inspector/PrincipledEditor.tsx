// The material inspector body: the inputs of the ONE Principled BSDF every shape renders with (PLAN §11), in Blender's
// panel groups (Paint, Base, Subsurface, Specular, Transmission, Coat, Sheen, Emission, Thin Film; presets.json
// "group"). Only groups that do something start expanded; every param and group resets to what it falls back to (the
// preset's starting value, or the layer material for a shape's own material).
import { useState, type ReactNode } from 'react'
import { ChevronRight, RotateCcw } from 'lucide-react'
import type { MaterialPreset, MaterialSpec, ParamSchema, Presets } from '../../../types'
import { cn, formatNumber } from '../../../lib/format'
import { withParam } from '../../../lib/projectOps'
import { ColorField, Row, Segmented, Select, SliderRow, Switch, type ChangePhase } from '../../../components/ui'
import {
  fallbackParam,
  groupActive,
  groupGate,
  optionLabel,
  PARAM_NEEDS,
  paramOverridden,
  principledGroups,
  resolvedParam,
  type ParamGroup,
  type ParamValue,
} from './principled'

/** Label column inside the panel: Blender's input names ("Use Art Colour As", "Anisotropic Rotation") need room. */
const LABEL_W = 104

/** Keys an edit touched ('*' = every param reset). */
export type MaterialEditKeys = string[] | '*'

export interface PrincipledEditorProps {
  presets: Presets
  /** The material written by edits (a layer's, a shape's own, the plate's). */
  spec: MaterialSpec
  /** Values `spec` inherits for params it does not set (a shape's own material: the layer material, same preset). */
  inherited?: MaterialSpec | null
  onChange: (next: MaterialSpec, phase: ChangePhase, keys: MaterialEditKeys) => void
  /** What resets return to (wording only): the preset's starting values or the layer material. */
  resetTo?: 'preset' | 'layer'
  /** Distinguishes edited objects so group expansion is recomputed when the target changes. */
  stateKey?: string
  disabled?: boolean
}

export function PrincipledEditor(p: PrincipledEditorProps) {
  // Re-key per edited object and preset: which groups start open is decided from the values at that moment
  // (dragging a weight to 0 must not collapse the group under the pointer).
  return <PrincipledPanel key={`${p.stateKey ?? ''}|${p.spec.preset}`} {...p} />
}

function PrincipledPanel({ presets, spec, inherited, onChange, resetTo = 'preset', disabled }: PrincipledEditorProps) {
  const preset: MaterialPreset | undefined = presets.materials[spec.preset]
  const groups = principledGroups(presets, preset)
  const value = (key: string) => resolvedParam(preset, spec, key, inherited)
  const overridden = (key: string) => paramOverridden(preset, spec, key, inherited)
  const [open, setOpen] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(groups.map((g) => [g.name, groupActive(g, value) || g.params.some(([k]) => overridden(k))])),
  )
  if (!preset) return <p className="text-3xs text-fg-4">Unknown material preset “{spec.preset}”.</p>
  if (!groups.length) return <p className="text-3xs text-fg-4">This material has no adjustable inputs.</p>

  const resetWord = resetTo === 'layer' ? 'the layer material' : `${preset.label}’s value`
  const set = (key: string, v: ParamValue | undefined, phase: ChangePhase) => onChange(withParam(spec, key, v), phase, [key])
  const resetKeys = (keys: string[]) => {
    let next = spec
    for (const k of keys) next = withParam(next, k, undefined)
    onChange(next, 'commit', keys)
  }
  const anyOverridden = groups.some((g) => g.params.some(([k]) => overridden(k)))

  return (
    <div className={cn('overflow-hidden rounded-lg border border-line bg-surface-0/40', disabled && 'pointer-events-none opacity-45')} data-principled="">
      <div className="flex h-7 items-center gap-1.5 border-b border-line bg-white/[0.025] pl-2 pr-1.5">
        <span className="h-2 w-2 shrink-0 rounded-full bg-[#4fa868] shadow-[0_0_6px_rgb(79_168_104/0.6)]" />
        <span
          className="min-w-0 flex-1 truncate text-2xs font-semibold text-fg-2"
          data-tip="Every shape renders with ONE Principled BSDF; these are its inputs. Cycles does the physics: refraction, real shadows, subsurface, thin film."
        >
          Principled BSDF
        </span>
        {anyOverridden && (
          <button
            type="button"
            onClick={() => onChange({ ...spec, params: {} }, 'commit', '*')}
            data-tip={`Reset every input to ${resetWord}`}
            className="flex h-5 items-center gap-1 rounded px-1.5 text-3xs text-fg-4 transition-colors hover:bg-white/[0.06] hover:text-fg-2"
          >
            <RotateCcw className="h-2.5 w-2.5" /> Reset all
          </button>
        )}
      </div>
      {groups.map((g) => (
        <GroupPanel
          key={g.name}
          group={g}
          open={!!open[g.name]}
          onToggle={() => setOpen((o) => ({ ...o, [g.name]: !o[g.name] }))}
          active={groupActive(g, value)}
          gate={groupGate(g)}
          value={value}
          overridden={overridden}
          onReset={() => resetKeys(g.params.map(([k]) => k).filter(overridden))}
          resetWord={resetWord}
        >
          {g.params.map(([key, schema]) => (
            <ParamRow
              key={key}
              paramKey={key}
              schema={schema}
              value={value(key)}
              fallback={fallbackParam(preset, spec, key, inherited)}
              overridden={overridden(key)}
              needsOff={PARAM_NEEDS[key] ? Number(value(PARAM_NEEDS[key].key)) <= 0 : false}
              resetWord={resetWord}
              onChange={(v, phase) => set(key, v, phase)}
              onReset={() => resetKeys([key])}
            />
          ))}
          {g.name === 'Emission' && (
            <p className="text-3xs leading-snug text-fg-4" style={{ paddingLeft: LABEL_W + 8 }}>
              Colour: {String(value('paintMode')).includes('emission') ? 'the art colour' : 'white (set Paint ▸ Use Art Colour As to light with the art)'}
            </p>
          )}
        </GroupPanel>
      ))}
    </div>
  )
}

function GroupPanel({
  group,
  open,
  onToggle,
  active,
  gate,
  value,
  overridden,
  onReset,
  resetWord,
  children,
}: {
  group: ParamGroup
  open: boolean
  onToggle: () => void
  active: boolean
  gate: string | null
  value: (key: string) => ParamValue
  overridden: (key: string) => boolean
  onReset: () => void
  resetWord: string
  children: ReactNode
}) {
  const modified = group.params.some(([k]) => overridden(k))
  const gateSchema = gate ? group.params.find(([k]) => k === gate)?.[1] : undefined
  const summary = gate ? (active ? formatValue(Number(value(gate)), gateSchema) : 'off') : null
  return (
    <div className="border-b border-line/70 last:border-b-0" data-group={group.name}>
      <div className="group/grp flex h-7 items-center gap-1 pl-1.5 pr-2">
        <button type="button" onClick={onToggle} aria-expanded={open} className="flex min-w-0 flex-1 items-center gap-1.5 text-left">
          <ChevronRight className={cn('h-3 w-3 shrink-0 text-fg-4 transition-transform duration-150', open && 'rotate-90')} />
          <span className={cn('truncate text-2xs', active ? 'font-medium text-fg-2' : 'text-fg-4')}>{group.name}</span>
          {modified && <span className="h-1 w-1 shrink-0 rounded-full bg-accent/80" />}
        </button>
        {modified && (
          <button
            type="button"
            onClick={onReset}
            data-tip={`Reset ${group.name} to ${resetWord}`}
            aria-label={`Reset ${group.name}`}
            className="opacity-0 transition-opacity focus-visible:opacity-100 group-hover/grp:opacity-100"
          >
            <RotateCcw className="h-2.5 w-2.5 text-fg-4 hover:text-fg-2" />
          </button>
        )}
        {summary && <span className={cn('shrink-0 text-3xs tabular', active ? 'text-fg-3' : 'text-fg-4/70')}>{summary}</span>}
      </div>
      {open && <div className="space-y-[7px] px-2 pb-2.5 pt-0.5 animate-fade-in">{children}</div>}
    </div>
  )
}

function formatValue(v: number, schema: ParamSchema | undefined): string {
  const step = schema?.step ?? 0.01
  const decimals = step >= 1 ? 0 : step >= 0.1 ? 1 : 2
  return `${formatNumber(v, decimals)}${schema?.unit ? ` ${schema.unit}` : ''}`
}

function ParamRow({
  paramKey,
  schema,
  value,
  fallback,
  overridden,
  needsOff,
  resetWord,
  onChange,
  onReset,
}: {
  paramKey: string
  schema: ParamSchema
  value: ParamValue
  fallback: ParamValue
  overridden: boolean
  needsOff: boolean
  resetWord: string
  onChange: (v: ParamValue, phase: ChangePhase) => void
  onReset: () => void
}) {
  const hint = [schema.help, needsOff ? PARAM_NEEDS[paramKey]?.hint : null].filter(Boolean).join(' ') || undefined
  const resetTip = `Reset to ${resetWord}`
  switch (schema.type) {
    case 'number':
      return (
        <SliderRow
          label={schema.label}
          hint={hint}
          value={Number(value)}
          min={schema.min ?? 0}
          max={schema.max ?? 1}
          step={schema.step ?? 0.01}
          unit={schema.unit}
          defaultValue={Number(fallback)}
          labelWidth={LABEL_W}
          modified={overridden}
          disabled={needsOff}
          onReset={onReset}
          resetTip={resetTip}
          onChange={(v, phase) => onChange(v, phase)}
        />
      )
    case 'enum': {
      const opts = (schema.options ?? []).map((o) => ({ value: o, label: optionLabel(paramKey, o) }))
      return (
        <Row label={schema.label} hint={hint} modified={overridden} onReset={onReset} resetTip={resetTip} labelWidth={LABEL_W}>
          {opts.length <= 3 ? (
            <Segmented size="xs" fill value={String(value)} options={opts} onChange={(v) => onChange(v, 'commit')} />
          ) : (
            <Select value={String(value)} options={opts} onChange={(v) => onChange(v, 'commit')} className="flex-1" />
          )}
        </Row>
      )
    }
    case 'bool':
      return (
        <Row label={schema.label} hint={hint} modified={overridden} onReset={onReset} resetTip={resetTip} labelWidth={LABEL_W}>
          <Switch checked={Boolean(value)} onChange={(v) => onChange(v, 'commit')} label={schema.label} />
        </Row>
      )
    case 'color':
      return (
        <Row label={schema.label} hint={hint} modified={overridden} onReset={onReset} resetTip={resetTip} labelWidth={LABEL_W}>
          <ColorField value={String(value)} onChange={(c, phase) => onChange(c, phase)} className="flex-1" />
        </Row>
      )
  }
}

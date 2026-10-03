// Material gallery (grouped by category, rendered swatches with CSS fallback, engine badges) + preset params.
import { useMemo, useState } from 'react'
import { ChevronDown, Info, RotateCcw } from 'lucide-react'
import type { MaterialPreset, MaterialSpec, Presets } from '../../../types'
import { cn } from '../../../lib/format'
import { CATEGORY_ORDER, MATERIAL_CSS } from '../../../lib/meta'
import { isParamModified, paramValue, withParam } from '../../../lib/projectOps'
import { isReservedParam } from '../../../lib/looks'
import { useUi } from '../../../store/ui'
import { ColorField, Row, Segmented, Select, SliderRow, Switch, type ChangePhase } from '../../../components/ui'

/** Rendered swatch (B's Cycles PNG) over a CSS fallback. `className` must position/size it (relative or absolute). */
export function MaterialSwatch({ id, preset, className }: { id: string; preset?: MaterialPreset; className?: string }) {
  const [failed, setFailed] = useState(false)
  return (
    <span className={cn('block overflow-hidden', className)}>
      <span className="absolute inset-0" style={{ background: MATERIAL_CSS[id] ?? 'linear-gradient(135deg,#555,#222)' }} />
      {preset?.swatch && !failed && (
        <img src={preset.swatch} alt="" draggable={false} loading="lazy" onError={() => setFailed(true)} className="absolute inset-0 h-full w-full object-cover" />
      )}
    </span>
  )
}

function engineBadge(p: MaterialPreset): { text: string; tone: string; tip: string } | null {
  if (p.engines.eevee === 'fallback')
    return { text: 'Cycles', tone: 'bg-warn/90 text-black', tip: p.eeveeNote ?? 'Full effect only in Cycles; EEVEE drafts use a fallback.' }
  if (p.eeveeNote) return { text: 'EEVEE≈', tone: 'bg-white/80 text-black', tip: p.eeveeNote }
  return null
}

export function MaterialGallery({
  value,
  onChange,
  presets,
  galleryId = 'gallery',
  defaultOpen = true,
}: {
  value: string
  onChange: (id: string) => void
  presets: Presets
  galleryId?: string
  defaultOpen?: boolean
}) {
  const open = useUi((s) => !(s.collapsedSections[galleryId] ?? !defaultOpen))
  const toggle = (id: string) => useUi.setState((s) => ({ collapsedSections: { ...s.collapsedSections, [id]: open } }))
  const groups = useMemo(() => {
    const by = new Map<string, [string, MaterialPreset][]>()
    for (const [id, m] of Object.entries(presets.materials)) {
      const list = by.get(m.category) ?? []
      list.push([id, m])
      by.set(m.category, list)
    }
    const order = [...CATEGORY_ORDER, ...[...by.keys()].filter((c) => !CATEGORY_ORDER.includes(c))]
    return order.filter((c) => by.has(c)).map((c) => [c, by.get(c)!] as const)
  }, [presets])
  const current = presets.materials[value]
  const badge = current ? engineBadge(current) : null

  return (
    <div className="space-y-2">
      {/* current material summary */}
      <button
        type="button"
        onClick={() => toggle(galleryId)}
        className="flex w-full items-center gap-2.5 rounded-lg border border-line bg-surface-0/60 p-1.5 pr-2 text-left transition-colors hover:border-line-2"
      >
        <MaterialSwatch id={value} preset={current} className="relative h-10 w-10 shrink-0 rounded-md" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span className="truncate text-xs font-semibold text-fg">{current?.label ?? value}</span>
            {badge && <span className={cn('rounded-[3px] px-1 text-[8.5px] font-bold tracking-wide', badge.tone)}>{badge.text}</span>}
          </div>
          <div className="line-clamp-2 text-3xs leading-snug text-fg-4">{current?.description ?? 'Unknown preset'}</div>
        </div>
        <ChevronDown className={cn('h-3.5 w-3.5 shrink-0 text-fg-4 transition-transform', open && 'rotate-180')} />
      </button>

      {open && (
        <div className="space-y-2 animate-fade-in">
          {groups.map(([cat, list]) => (
            <div key={cat}>
              <div className="mb-1 text-3xs font-semibold uppercase tracking-[0.09em] text-fg-4">{cat}</div>
              <div className="grid grid-cols-5 gap-1.5">
                {list.map(([id, m]) => {
                  const b = engineBadge(m)
                  const active = id === value
                  return (
                    <button
                      key={id}
                      type="button"
                      onClick={() => onChange(id)}
                      data-tip={`${m.label} — ${m.description}${b ? ` (${b.tip})` : ''}`}
                      aria-label={m.label}
                      aria-pressed={active}
                      className="group flex min-w-0 flex-col items-center gap-1"
                    >
                      <span
                        className={cn(
                          'relative block aspect-square w-full overflow-hidden rounded-[9px] border transition-[box-shadow,transform,border-color] duration-150',
                          active ? 'border-transparent shadow-[0_0_0_2px_var(--color-accent)]' : 'border-line-2 group-hover:-translate-y-px group-hover:border-line-3',
                        )}
                      >
                        <MaterialSwatch id={id} preset={m} className="absolute inset-0" />
                        {b && <span className={cn('absolute right-0.5 top-0.5 rounded-[3px] px-[3px] text-[7px] font-bold leading-[11px]', b.tone)}>{b.text === 'Cycles' ? 'C' : '≈'}</span>}
                      </span>
                      <span className={cn('w-full truncate text-center text-[9.5px] leading-tight', active ? 'text-fg' : 'text-fg-4 group-hover:text-fg-2')}>{m.label}</span>
                    </button>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      )}
      {current?.eeveeNote && (
        <div className="flex items-start gap-1.5 rounded-md border border-warn/20 bg-warn/[0.07] px-2 py-1.5 text-3xs leading-snug text-warn/90">
          <Info className="mt-px h-3 w-3 shrink-0" />
          <span>
            <b className="font-semibold">Draft note:</b> {current.eeveeNote}
          </span>
        </div>
      )}
    </div>
  )
}

export function MaterialParams({
  preset,
  spec,
  onChange,
  disabled,
}: {
  preset: MaterialPreset | undefined
  spec: MaterialSpec
  onChange: (spec: MaterialSpec, phase: ChangePhase, key: string) => void
  disabled?: boolean
}) {
  if (!preset) return null
  // Reserved `__*` keys are render-time intent flags (lib/appearance.ts) — never user-facing.
  const entries = Object.entries(preset.params).filter(([k]) => !isReservedParam(k))
  if (!entries.length) return <p className="text-3xs text-fg-4">This material has no adjustable parameters.</p>
  const anyModified = entries.some(([k]) => isParamModified(preset, spec, k))
  return (
    <div className={cn('space-y-[7px]', disabled && 'pointer-events-none opacity-45')}>
      {entries.map(([key, schema]) => {
        const v = paramValue(preset, spec, key)
        const modified = isParamModified(preset, spec, key)
        const reset = () => onChange(withParam(spec, key, undefined), 'commit', key)
        switch (schema.type) {
          case 'number':
            return (
              <SliderRow
                key={key}
                label={schema.label}
                hint={schema.help}
                value={Number(v)}
                min={schema.min ?? 0}
                max={schema.max ?? 1}
                step={schema.step ?? 0.01}
                unit={schema.unit && schema.unit !== 'IOR' ? schema.unit : undefined}
                defaultValue={Number(schema.default)}
                modified={modified}
                onChange={(nv, phase) => onChange(withParam(spec, key, nv), phase, key)}
              />
            )
          case 'enum': {
            const opts = schema.options ?? []
            return (
              <Row key={key} label={schema.label} hint={schema.help} modified={modified} onReset={reset}>
                {opts.length <= 4 ? (
                  <Segmented
                    size="xs"
                    fill
                    value={String(v)}
                    options={opts.map((o) => ({ value: o, label: o }))}
                    onChange={(nv) => onChange(withParam(spec, key, nv), 'commit', key)}
                  />
                ) : (
                  <Select value={String(v)} options={opts.map((o) => ({ value: o, label: o }))} onChange={(nv) => onChange(withParam(spec, key, nv), 'commit', key)} className="flex-1" />
                )}
              </Row>
            )
          }
          case 'bool':
            return (
              <Row key={key} label={schema.label} hint={schema.help} modified={modified} onReset={reset}>
                <Switch checked={Boolean(v)} onChange={(nv) => onChange(withParam(spec, key, nv), 'commit', key)} label={schema.label} />
              </Row>
            )
          case 'color':
            return (
              <Row key={key} label={schema.label} hint={schema.help} modified={modified} onReset={reset}>
                <ColorField value={String(v)} onChange={(c, phase) => onChange(withParam(spec, key, c), phase, key)} className="flex-1" />
              </Row>
            )
        }
      })}
      {anyModified && (
        <button
          type="button"
          onClick={() => onChange({ ...spec, params: {} }, 'commit', '*')}
          className="ml-[92px] flex items-center gap-1 text-3xs text-fg-4 transition-colors hover:text-fg-2"
        >
          <RotateCcw className="h-2.5 w-2.5" /> Reset all to preset defaults
        </button>
      )}
    </div>
  )
}

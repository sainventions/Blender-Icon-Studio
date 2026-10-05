// Per-appearance override scope (Icon Composer's section scope pop-up + nested override rows).
import { useRef, type ReactNode } from 'react'
import { ChevronDown, CornerDownRight, Globe2, X } from 'lucide-react'
import type { Fill, LayerOverride, MaterialSpec, Presets, Project } from '../../../types'
import { cn } from '../../../lib/format'
import { fillToCss } from '../../../lib/color'
import { BUCKET_LABEL, clearLayerOverride, overrideBucket, overridesOf, setPlateFillOverride, type OverrideBucket, type OverrideField } from '../../../lib/projectOps'
import { useEditor } from '../../../store/editor'
import { useUi, type ScopeSection } from '../../../store/ui'
import { Menu, useAnchor } from '../../../components/ui'
import { AppearanceIcon } from '../../../components/icons'

export interface EffectiveScope {
  /** Override bucket to write into (null = base values). */
  bucket: OverrideBucket | null
  /** The user's choice for the section (meaningful only when the appearance has a bucket). */
  scope: 'all' | 'appearance'
}

export function useScope(section: ScopeSection): EffectiveScope {
  const appearance = useEditor((s) => s.project!.appearance)
  const scope = useUi((s) => s.scopes[section])
  const bucket = overrideBucket(appearance)
  return { bucket: bucket && scope === 'appearance' ? bucket : null, scope: bucket ? scope : 'all' }
}

const BUCKET_HINT: Record<OverrideBucket, string> = {
  dark: 'Dark rendition only',
  mono: 'Mono annotation, used by Clear and Tinted renditions',
}

export function ScopePicker({ section }: { section: ScopeSection }) {
  const appearance = useEditor((s) => s.project!.appearance)
  const scope = useUi((s) => s.scopes[section])
  const setScope = useUi((s) => s.setScope)
  const bucket = overrideBucket(appearance)
  const ref = useRef<HTMLButtonElement>(null)
  const menu = useAnchor<HTMLButtonElement>()
  const effective = bucket ? scope : 'all'

  return (
    <>
      <button
        ref={ref}
        type="button"
        disabled={!bucket}
        onClick={() => ref.current && menu.toggle(ref.current)}
        data-tip={bucket ? 'Where edits in this section go' : 'Switch to Dark / Clear / Tinted to vary values per appearance'}
        className={cn(
          'flex h-5 items-center gap-1 rounded-[5px] border px-1.5 text-3xs font-semibold transition-colors',
          effective === 'appearance'
            ? 'border-accent/40 bg-accent/15 text-[#cfc7ff] hover:bg-accent/25'
            : 'border-line bg-white/[0.03] text-fg-3 hover:text-fg-2 disabled:opacity-60',
        )}
      >
        {effective === 'appearance' && bucket ? (
          <>
            <AppearanceIcon appearance={appearance} className="h-2.5 w-2.5" /> {BUCKET_LABEL[bucket]}
          </>
        ) : (
          <>
            <Globe2 className="h-2.5 w-2.5" /> All
          </>
        )}
        {bucket && <ChevronDown className="h-2.5 w-2.5 opacity-70" />}
      </button>
      {bucket && (
        <Menu
          open={menu.open}
          onClose={menu.close}
          anchor={menu.anchor}
          placement="bottom-end"
          width={250}
          items={[
            { type: 'label', label: 'Apply edits to' },
            { label: 'All appearances', description: 'Edit the base values.', checked: scope === 'all', icon: <Globe2 />, onSelect: () => setScope(section, 'all') },
            {
              label: `${BUCKET_LABEL[bucket]} only`,
              description: BUCKET_HINT[bucket],
              checked: scope === 'appearance',
              icon: <AppearanceIcon appearance={appearance} />,
              onSelect: () => setScope(section, 'appearance'),
            },
          ]}
        />
      )}
    </>
  )
}

function formatOverride(field: OverrideField, value: unknown, presets: Presets | null): ReactNode {
  switch (field) {
    case 'opacity':
      return `${Math.round((value as number) * 100)}%`
    case 'visible':
      return value ? 'Visible' : 'Hidden'
    case 'blendMode':
      return String(value)
    case 'material':
      return presets?.materials[(value as MaterialSpec).preset]?.label ?? (value as MaterialSpec).preset
    case 'fill':
      return <FillChipText fill={value as Fill} />
  }
}

function FillChipText({ fill }: { fill: Fill }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span className="relative inline-block h-2.5 w-2.5 overflow-hidden rounded-[3px] border border-white/20 checkerboard-sm">
        <span className="absolute inset-0" style={{ background: fillToCss(fill) }} />
      </span>
      {fill.type}
    </span>
  )
}

/** Nested "↳ Dark: 60%  ×" rows under a property when editing base values. */
export function OverrideRows({
  project,
  layerIds,
  field,
  presets,
}: {
  project: Project
  layerIds: string[]
  field: OverrideField
  presets: Presets | null
}) {
  const primary = layerIds[layerIds.length - 1]
  if (!primary) return null
  const rows = overridesOf(project, primary, field)
  if (!rows.length) return null
  return (
    <div className="-mt-0.5 space-y-0.5 pl-[84px]">
      {rows.map((r) => (
        <div key={r.bucket} className="group flex h-5 items-center gap-1.5 rounded px-1 text-3xs text-fg-3 hover:bg-white/[0.04]">
          <CornerDownRight className="h-2.5 w-2.5 text-fg-4" />
          <span className="font-semibold text-[#cfc7ff]">{BUCKET_LABEL[r.bucket]}</span>
          <span className="min-w-0 flex-1 truncate text-fg-2">{formatOverride(field, r.value, presets)}</span>
          <button
            type="button"
            data-tip={`Remove ${BUCKET_LABEL[r.bucket]} override`}
            onClick={() => useEditor.getState().commit((p) => clearLayerOverride(p, r.bucket, layerIds, field))}
            className="flex h-4 w-4 items-center justify-center rounded text-fg-4 opacity-0 transition-opacity hover:bg-white/10 hover:text-fg group-hover:opacity-100"
          >
            <X className="h-2.5 w-2.5" />
          </button>
        </div>
      ))}
    </div>
  )
}

export function PlateFillOverrideRows({ project }: { project: Project }) {
  const rows = (['dark', 'mono'] as const).filter((b) => project.appearances[b]?.plateFill)
  if (!rows.length) return null
  return (
    <div className="space-y-0.5 pl-[84px]">
      {rows.map((b) => (
        <div key={b} className="group flex h-5 items-center gap-1.5 rounded px-1 text-3xs text-fg-3 hover:bg-white/[0.04]">
          <CornerDownRight className="h-2.5 w-2.5 text-fg-4" />
          <span className="font-semibold text-[#cfc7ff]">{BUCKET_LABEL[b]}</span>
          <span className="min-w-0 flex-1 truncate text-fg-2">
            <FillChipText fill={project.appearances[b].plateFill!} />
          </span>
          <button
            type="button"
            data-tip={`Remove ${BUCKET_LABEL[b]} plate fill`}
            onClick={() => useEditor.getState().commit((p) => setPlateFillOverride(p, b, null))}
            className="flex h-4 w-4 items-center justify-center rounded text-fg-4 opacity-0 transition-opacity hover:bg-white/10 hover:text-fg group-hover:opacity-100"
          >
            <X className="h-2.5 w-2.5" />
          </button>
        </div>
      ))}
    </div>
  )
}

export type { LayerOverride }

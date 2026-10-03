// Look cards + gallery grid (Document inspector, top-bar Looks popover, Icon Pack page).
import type { ReactNode } from 'react'
import { Check, LoaderCircle } from 'lucide-react'
import type { Look, Presets } from '../../types'
import { cn } from '../../lib/format'
import { lookEntries } from '../../lib/looks'
import { Skeleton } from '../../components/ui'
import { LookThumb } from './LookThumb'

export function LookCard({
  id,
  look,
  presets,
  active,
  busy,
  disabled,
  variant = 'full',
  onPick,
  onHover,
}: {
  id: string
  look: Look
  presets: Presets | null
  active?: boolean
  busy?: boolean
  disabled?: boolean
  variant?: 'full' | 'compact'
  onPick: (id: string) => void
  onHover?: (id: string | null) => void
}) {
  const compact = variant === 'compact'
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => onPick(id)}
      onPointerEnter={() => onHover?.(id)}
      onPointerLeave={() => onHover?.(null)}
      onFocus={() => onHover?.(id)}
      onBlur={() => onHover?.(null)}
      aria-pressed={active}
      aria-label={`${look.label} look — ${look.description}`}
      data-tip={compact && !onHover ? look.description : undefined}
      className={cn(
        'group flex min-w-0 flex-col text-left outline-none disabled:cursor-not-allowed disabled:opacity-50',
        compact ? 'items-center gap-1' : 'gap-1.5 rounded-xl p-1.5 transition-colors hover:bg-white/[0.035] focus-visible:bg-white/[0.04]',
        !compact && active && 'bg-accent/[0.07]',
      )}
    >
      <span
        className={cn(
          'relative block w-full overflow-hidden border transition-[box-shadow,transform,border-color] duration-150',
          compact ? 'aspect-square rounded-[10px]' : 'aspect-[4/3] rounded-[10px]',
          active
            ? 'border-transparent shadow-[0_0_0_2px_var(--color-accent)]'
            : 'border-line-2 group-hover:-translate-y-px group-hover:border-line-3 group-focus-visible:shadow-[0_0_0_2px_color-mix(in_srgb,var(--color-accent)_70%,transparent)]',
        )}
      >
        <LookThumb look={look} presets={presets} className="absolute inset-0" />
        {active && !busy && (
          <span className="absolute right-1 top-1 flex h-4 w-4 items-center justify-center rounded-full accent-gradient shadow-[0_1px_4px_rgb(0_0_0/0.5)]">
            <Check className="h-2.5 w-2.5 text-white" strokeWidth={3} />
          </span>
        )}
        {busy && (
          <span className="absolute inset-0 flex items-center justify-center bg-black/45 backdrop-blur-[2px]">
            <LoaderCircle className="h-5 w-5 animate-spin text-white" />
          </span>
        )}
      </span>
      {compact ? (
        <span className={cn('w-full truncate text-center text-[10px] leading-tight', active ? 'font-medium text-fg' : 'text-fg-3 group-hover:text-fg-2')}>{look.label}</span>
      ) : (
        <span className="min-w-0 px-0.5">
          <span className={cn('block truncate text-xs font-semibold', active ? 'text-fg' : 'text-fg-2 group-hover:text-fg')}>{look.label}</span>
          <span className="mt-0.5 line-clamp-2 block text-3xs leading-snug text-fg-4">{look.description}</span>
        </span>
      )}
    </button>
  )
}

export function LooksGrid({
  presets,
  value,
  busyId,
  disabled,
  onPick,
  onHover,
  variant = 'full',
  columns = 3,
  before,
  className,
}: {
  presets: Presets | null
  value: string | null
  busyId?: string | null
  disabled?: boolean
  onPick: (id: string) => void
  onHover?: (id: string | null) => void
  variant?: 'full' | 'compact'
  columns?: number
  /** Extra tiles rendered before the looks (e.g. "Keep each icon's style"). */
  before?: ReactNode
  className?: string
}) {
  const looks = lookEntries(presets)
  if (!presets) {
    // Presets still loading: placeholder tiles in the final layout (no jump when they arrive).
    return (
      <div className={cn('grid', variant === 'compact' ? 'gap-x-1.5 gap-y-2' : 'gap-1', className)} style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
        {Array.from({ length: columns * 2 }).map((_, i) => (
          <div key={i} className={cn('flex flex-col gap-1.5', variant === 'full' && 'p-1.5')}>
            <Skeleton className={cn('w-full rounded-[10px]', variant === 'compact' ? 'aspect-square' : 'aspect-[4/3]')} />
            <Skeleton className="mx-auto h-2 w-2/3" />
          </div>
        ))}
      </div>
    )
  }
  if (!looks.length && !before) {
    return <div className="rounded-lg border border-dashed border-line-2 px-3 py-5 text-center text-2xs text-fg-4">No looks available — the server's presets.json has no "looks".</div>
  }
  return (
    <div className={cn('grid', variant === 'compact' ? 'gap-x-1.5 gap-y-2' : 'gap-1', className)} style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
      {before}
      {looks.map(([id, look]) => (
        <LookCard
          key={id}
          id={id}
          look={look}
          presets={presets}
          variant={variant}
          active={value === id}
          busy={busyId === id}
          disabled={disabled || (!!busyId && busyId !== id)}
          onPick={onPick}
          onHover={onHover}
        />
      ))}
    </div>
  )
}

// Small presentational primitives: Kbd, Badge, Skeleton, ProgressRing, EmptyState, StatusDot, Spinner.
import { useId, type ReactNode } from 'react'
import { LoaderCircle } from 'lucide-react'
import { cn, prettyShortcut } from '../../lib/format'

export function Kbd({ children, className }: { children: string; className?: string }) {
  return (
    <kbd
      className={cn(
        'inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-[4px] border border-line-2 bg-white/[0.04] px-1 font-sans text-3xs font-medium text-fg-2 shadow-[0_1px_0_rgb(255_255_255/0.05)_inset]',
        className,
      )}
    >
      {prettyShortcut(children)}
    </kbd>
  )
}

type BadgeTone = 'neutral' | 'accent' | 'ok' | 'warn' | 'bad' | 'info'
const TONES: Record<BadgeTone, string> = {
  neutral: 'bg-white/[0.06] text-fg-3 border-line',
  accent: 'bg-accent/15 text-[#c4baff] border-accent/30',
  ok: 'bg-ok/12 text-ok border-ok/25',
  warn: 'bg-warn/12 text-warn border-warn/25',
  bad: 'bg-bad/12 text-bad border-bad/25',
  info: 'bg-info/12 text-info border-info/25',
}

export function Badge({ children, tone = 'neutral', className, tip }: { children: ReactNode; tone?: BadgeTone; className?: string; tip?: string }) {
  return (
    <span
      data-tip={tip}
      className={cn(
        'inline-flex h-4 shrink-0 items-center gap-1 whitespace-nowrap rounded-[4px] border px-1 text-3xs font-semibold tracking-[0.02em] [&_svg]:h-2.5 [&_svg]:w-2.5',
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  )
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn('skeleton rounded-md', className)} />
}

export function Spinner({ className }: { className?: string }) {
  return <LoaderCircle className={cn('h-4 w-4 animate-spin text-fg-3', className)} />
}

/** Circular progress (value 0..1) — indeterminate spinner arc when value is null. */
export function ProgressRing({
  value,
  size = 28,
  stroke = 2.5,
  className,
  children,
}: {
  value: number | null
  size?: number
  stroke?: number
  className?: string
  children?: ReactNode
}) {
  const id = useId().replace(/:/g, '')
  const r = (size - stroke) / 2
  const c = 2 * Math.PI * r
  const v = value == null ? 0.28 : Math.max(0.02, Math.min(1, value))
  return (
    <div className={cn('relative inline-flex items-center justify-center', className)} style={{ width: size, height: size }}>
      <svg width={size} height={size} className={cn('-rotate-90', value == null && 'animate-spin')} style={value == null ? { animationDuration: '0.9s' } : undefined}>
        <defs>
          <linearGradient id={`pr${id}`} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="var(--color-accent)" />
            <stop offset="100%" stopColor="var(--color-accent-2)" />
          </linearGradient>
        </defs>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="rgb(255 255 255 / 0.1)" strokeWidth={stroke} />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={`url(#pr${id})`}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={`${c * v} ${c}`}
          style={{ transition: value == null ? undefined : 'stroke-dasharray 250ms ease-out' }}
        />
      </svg>
      {children && <div className="absolute inset-0 flex items-center justify-center">{children}</div>}
    </div>
  )
}

export function ProgressBar({ value, className }: { value: number | null; className?: string }) {
  return (
    <div className={cn('relative h-1 w-full overflow-hidden rounded-full bg-white/[0.08]', className)}>
      {value == null ? (
        <div className="absolute inset-y-0 w-1/3 rounded-full accent-gradient animate-[indeterminate_1.2s_ease-in-out_infinite]" />
      ) : (
        <div className="absolute inset-y-0 left-0 rounded-full accent-gradient transition-[width] duration-300" style={{ width: `${Math.max(2, value * 100)}%` }} />
      )}
    </div>
  )
}

export function EmptyState({
  icon,
  title,
  children,
  action,
  className,
}: {
  icon?: ReactNode
  title: ReactNode
  children?: ReactNode
  action?: ReactNode
  className?: string
}) {
  return (
    <div className={cn('flex flex-col items-center justify-center px-6 py-8 text-center', className)}>
      {icon && (
        <div className="mb-3 flex h-10 w-10 items-center justify-center rounded-xl border border-line-2 bg-white/[0.03] text-fg-3 shadow-[0_1px_0_rgb(255_255_255/0.06)_inset] [&>svg]:h-[18px] [&>svg]:w-[18px]">
          {icon}
        </div>
      )}
      <div className="text-xs font-semibold text-fg-2">{title}</div>
      {children && <div className="mt-1 max-w-[260px] text-2xs leading-relaxed text-fg-3">{children}</div>}
      {action && <div className="mt-3">{action}</div>}
    </div>
  )
}

export type DotTone = 'ok' | 'warn' | 'bad' | 'idle' | 'busy'
export function StatusDot({ tone, pulse, className }: { tone: DotTone; pulse?: boolean; className?: string }) {
  const color = {
    ok: 'bg-ok shadow-[0_0_8px_rgb(61_220_151/0.7)]',
    warn: 'bg-warn shadow-[0_0_8px_rgb(245_185_66/0.6)]',
    bad: 'bg-bad shadow-[0_0_8px_rgb(255_107_107/0.6)]',
    idle: 'bg-fg-4',
    busy: 'bg-accent-2 shadow-[0_0_8px_rgb(47_212_240/0.7)]',
  }[tone]
  return <span className={cn('inline-block h-[7px] w-[7px] shrink-0 rounded-full', color, pulse && 'animate-pulse-dot', className)} />
}

// Inspector layout primitives: collapsible sections, labelled rows and composite slider rows.
import type { ReactNode } from 'react'
import { ChevronRight, RotateCcw } from 'lucide-react'
import { cn } from '../../lib/format'
import { useUi } from '../../store/ui'
import { NumberField, ScrubLabel, type ChangePhase } from './NumberField'
import { Slider } from './Slider'

export function Section({
  id,
  title,
  icon,
  right,
  children,
  className,
  defaultCollapsed = false,
}: {
  id: string
  title: ReactNode
  icon?: ReactNode
  right?: ReactNode
  children: ReactNode
  className?: string
  defaultCollapsed?: boolean
}) {
  const stored = useUi((s) => s.collapsedSections[id])
  const toggle = useUi((s) => s.toggleSection)
  const collapsed = stored === undefined ? defaultCollapsed : stored
  return (
    <section className={cn('border-b border-line', className)}>
      <div className="flex h-9 items-center gap-1.5 pl-2 pr-2.5">
        <button
          type="button"
          onClick={() => {
            if (stored === undefined && defaultCollapsed) useUi.setState((s) => ({ collapsedSections: { ...s.collapsedSections, [id]: false } }))
            else toggle(id)
          }}
          className="group flex min-w-0 flex-1 items-center gap-1.5 text-left"
          aria-expanded={!collapsed}
        >
          <ChevronRight className={cn('h-3 w-3 shrink-0 text-fg-4 transition-transform duration-200', !collapsed && 'rotate-90')} />
          {icon && <span className="flex h-3.5 w-3.5 items-center justify-center text-fg-3 [&>svg]:h-3.5 [&>svg]:w-3.5">{icon}</span>}
          <span className="truncate text-2xs font-semibold tracking-[0.01em] text-fg-2 group-hover:text-fg">{title}</span>
        </button>
        {right}
      </div>
      {!collapsed && <div className="space-y-[7px] px-3 pb-3.5 pt-0.5 animate-fade-in">{children}</div>}
    </section>
  )
}

export function Row({
  label,
  children,
  hint,
  modified,
  onReset,
  className,
  labelWidth = 84,
  align = 'center',
  resetTip = 'Reset to default',
}: {
  label: ReactNode
  children: ReactNode
  hint?: string
  modified?: boolean
  onReset?: () => void
  resetTip?: string
  className?: string
  labelWidth?: number
  align?: 'center' | 'start'
}) {
  return (
    <div className={cn('group/row flex min-h-6 gap-2', align === 'center' ? 'items-center' : 'items-start', className)}>
      <div className="flex shrink-0 items-center gap-1" style={{ width: labelWidth }} title={hint}>
        <span className={cn('truncate text-2xs', modified ? 'text-fg-2' : 'text-fg-3')}>{label}</span>
        {modified && onReset && (
          <button
            type="button"
            onClick={onReset}
            data-tip={resetTip}
            aria-label={resetTip}
            className="opacity-0 transition-opacity focus-visible:opacity-100 group-hover/row:opacity-100"
          >
            <RotateCcw className="h-2.5 w-2.5 text-fg-4 hover:text-fg-2" />
          </button>
        )}
        {modified && <span className="h-1 w-1 shrink-0 rounded-full bg-accent" />}
      </div>
      <div className="flex min-w-0 flex-1 items-center gap-1.5">{children}</div>
    </div>
  )
}

export interface SliderRowProps {
  label: ReactNode
  value: number
  min: number
  max: number
  step?: number
  defaultValue?: number
  unit?: string
  scale?: number
  decimals?: number
  hint?: string
  disabled?: boolean
  soft?: boolean
  origin?: number
  labelWidth?: number
  onChange: (v: number, phase: ChangePhase) => void
  trackBackground?: string
  modified?: boolean
  /** Shown as a hover reset button next to the label while modified (e.g. back to the preset's value). Double-click
   *  on the label / slider still resets to `defaultValue`. */
  onReset?: () => void
  resetTip?: string
}

/** Label (scrubbable) + slider + numeric field — the workhorse inspector row. */
export function SliderRow({
  label,
  value,
  min,
  max,
  step = 0.01,
  defaultValue,
  unit,
  scale = 1,
  decimals,
  hint,
  disabled,
  soft,
  origin,
  labelWidth = 84,
  onChange,
  trackBackground,
  modified,
  onReset,
  resetTip = 'Reset to default',
}: SliderRowProps) {
  const isModified = modified ?? (defaultValue !== undefined && Math.abs(value - defaultValue) > 1e-9)
  return (
    <div className={cn('group/row flex h-6 items-center gap-2', disabled && 'opacity-45')} title={hint}>
      <div className="flex shrink-0 items-center gap-1" style={{ width: labelWidth }}>
        <ScrubLabel
          value={value}
          onChange={onChange}
          step={step}
          min={soft ? -Infinity : min}
          max={soft ? Infinity : max}
          disabled={disabled}
          onReset={defaultValue !== undefined ? () => onChange(defaultValue, 'commit') : undefined}
          className={cn('text-2xs', isModified && 'text-fg-2')}
        >
          {label}
        </ScrubLabel>
        {isModified && onReset && !disabled && (
          <button type="button" onClick={onReset} data-tip={resetTip} aria-label={resetTip} className="opacity-0 transition-opacity focus-visible:opacity-100 group-hover/row:opacity-100">
            <RotateCcw className="h-2.5 w-2.5 text-fg-4 hover:text-fg-2" />
          </button>
        )}
        {isModified && <span className="h-1 w-1 shrink-0 rounded-full bg-accent/80" />}
      </div>
      <Slider
        value={value}
        min={min}
        max={max}
        step={step}
        defaultValue={defaultValue}
        onChange={onChange}
        disabled={disabled}
        origin={origin}
        trackBackground={trackBackground}
        ariaLabel={typeof label === 'string' ? label : undefined}
      />
      <NumberField
        value={value}
        onChange={onChange}
        min={min}
        max={max}
        step={step}
        scale={scale}
        decimals={decimals}
        unit={unit}
        soft={soft}
        disabled={disabled}
        className="w-[58px] shrink-0"
        ariaLabel={typeof label === 'string' ? label : undefined}
      />
    </div>
  )
}

export function SubLabel({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn('pt-1 text-3xs font-semibold uppercase tracking-[0.09em] text-fg-4', className)}>{children}</div>
}

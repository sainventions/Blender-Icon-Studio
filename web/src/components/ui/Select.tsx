import { useRef, type ReactNode } from 'react'
import { ChevronsUpDown } from 'lucide-react'
import { cn } from '../../lib/format'
import { Menu, useAnchor, type MenuItem } from './Menu'

export interface SelectOption<T extends string> {
  value: T
  label: ReactNode
  icon?: ReactNode
  description?: ReactNode
  disabled?: boolean
  group?: string
}

export function Select<T extends string>({
  value,
  options,
  onChange,
  className,
  placeholder = 'Select…',
  disabled,
  menuWidth,
  size = 'sm',
  ariaLabel,
}: {
  value: T | null
  options: SelectOption<T>[]
  onChange: (v: T) => void
  className?: string
  placeholder?: string
  disabled?: boolean
  menuWidth?: number
  size?: 'xs' | 'sm'
  ariaLabel?: string
}) {
  const btn = useRef<HTMLButtonElement>(null)
  const menu = useAnchor<HTMLButtonElement>()
  const cur = options.find((o) => o.value === value)
  const items: MenuItem[] = []
  let group: string | undefined
  for (const o of options) {
    if (o.group && o.group !== group) {
      group = o.group
      items.push({ type: 'label', label: o.group, key: `g-${o.group}` })
    }
    items.push({
      key: o.value,
      label: o.label,
      icon: o.icon,
      description: o.description,
      disabled: o.disabled,
      checked: o.value === value,
      onSelect: () => onChange(o.value),
    })
  }
  return (
    <>
      <button
        ref={btn}
        type="button"
        disabled={disabled}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        onClick={() => btn.current && menu.toggle(btn.current)}
        className={cn(
          'flex min-w-0 items-center gap-1.5 rounded-md border border-line bg-surface-0 pl-2 pr-1.5 text-left text-2xs text-fg transition-colors hover:border-line-2 disabled:opacity-45',
          size === 'sm' ? 'h-6' : 'h-5',
          menu.open && 'border-accent/60',
          className,
        )}
      >
        {cur?.icon && <span className="flex shrink-0 items-center text-fg-3 [&>svg]:h-3.5 [&>svg]:w-3.5">{cur.icon}</span>}
        <span className={cn('min-w-0 flex-1 truncate', !cur && 'text-fg-4')}>{cur ? cur.label : placeholder}</span>
        <ChevronsUpDown className="h-3 w-3 shrink-0 text-fg-4" />
      </button>
      <Menu open={menu.open} onClose={menu.close} anchor={menu.anchor} items={items} matchWidth width={menuWidth} />
    </>
  )
}

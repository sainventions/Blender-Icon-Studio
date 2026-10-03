// Dropdown / context menu with keyboard navigation, checks, shortcuts and descriptions.
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Check } from 'lucide-react'
import { cn, prettyShortcut } from '../../lib/format'
import { Popover, type Anchor, type Placement } from './Popover'

export type MenuItem =
  | {
      type?: 'item'
      key?: string
      label: ReactNode
      icon?: ReactNode
      shortcut?: string
      description?: ReactNode
      onSelect: () => void
      disabled?: boolean
      danger?: boolean
      checked?: boolean
      right?: ReactNode
    }
  | { type: 'separator'; key?: string }
  | { type: 'label'; label: ReactNode; key?: string }

export interface MenuProps {
  open: boolean
  onClose: () => void
  anchor: Anchor
  items: MenuItem[]
  placement?: Placement
  className?: string
  width?: number
  matchWidth?: boolean
  header?: ReactNode
}

export function Menu({ open, onClose, anchor, items, placement = 'bottom-start', className, width, matchWidth, header }: MenuProps) {
  const [active, setActive] = useState(-1)
  const listRef = useRef<HTMLDivElement>(null)
  const actionable = items
    .map((it, i) => ({ it, i }))
    .filter(({ it }) => (it.type ?? 'item') === 'item' && !(it as { disabled?: boolean }).disabled)
    .map(({ i }) => i)

  useEffect(() => {
    if (!open) return
    setActive(-1)
    requestAnimationFrame(() => listRef.current?.focus())
  }, [open])

  const select = (i: number) => {
    const it = items[i]
    if (!it || (it.type ?? 'item') !== 'item') return
    const item = it as Extract<MenuItem, { onSelect: () => void }>
    if (item.disabled) return
    onClose()
    // Let the menu close before running (focus, dialogs).
    requestAnimationFrame(() => item.onSelect())
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!actionable.length) return
    const pos = actionable.indexOf(active)
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActive(actionable[(pos + 1) % actionable.length])
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActive(actionable[(pos - 1 + actionable.length) % actionable.length])
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      if (active >= 0) select(active)
    } else if (e.key === 'Home') {
      setActive(actionable[0])
    } else if (e.key === 'End') {
      setActive(actionable[actionable.length - 1])
    }
  }

  return (
    <Popover open={open} onClose={onClose} anchor={anchor} placement={placement} matchWidth={matchWidth} role="menu" className={cn('p-1', className)} style={{ width }}>
      {header}
      <div ref={listRef} tabIndex={-1} onKeyDown={onKeyDown} className="max-h-[min(70vh,520px)] overflow-y-auto outline-none">
        {items.map((it, i) => {
          const type = it.type ?? 'item'
          if (type === 'separator') return <div key={it.key ?? `sep${i}`} className="mx-1 my-1 h-px bg-line-2" />
          if (type === 'label')
            return (
              <div key={it.key ?? `lbl${i}`} className="px-2 pb-1 pt-2 text-3xs font-semibold uppercase tracking-[0.08em] text-fg-4">
                {(it as { label: ReactNode }).label}
              </div>
            )
          const item = it as Extract<MenuItem, { onSelect: () => void }>
          return (
            <button
              key={item.key ?? i}
              type="button"
              role="menuitem"
              disabled={item.disabled}
              onPointerEnter={() => setActive(i)}
              onPointerLeave={() => setActive(-1)}
              onClick={() => select(i)}
              className={cn(
                'flex w-full items-start gap-2 rounded-md px-2 py-[5px] text-left text-xs transition-colors',
                item.disabled ? 'cursor-not-allowed opacity-40' : active === i ? (item.danger ? 'bg-bad/15 text-bad' : 'bg-white/[0.07] text-fg') : item.danger ? 'text-bad/90' : 'text-fg-2',
              )}
            >
              <span className="mt-[1px] flex h-4 w-4 shrink-0 items-center justify-center text-fg-3 [&>svg]:h-[14px] [&>svg]:w-[14px]">
                {item.checked ? <Check className="text-accent" /> : item.icon}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium">{item.label}</span>
                {item.description && <span className="mt-0.5 block text-2xs leading-snug text-fg-3">{item.description}</span>}
              </span>
              {item.right}
              {item.shortcut && <span className="ml-3 mt-[1px] shrink-0 text-2xs text-fg-4">{prettyShortcut(item.shortcut)}</span>}
            </button>
          )
        })}
      </div>
    </Popover>
  )
}

/** Convenience: state for an anchored menu. */
export function useAnchor<T extends HTMLElement = HTMLElement>() {
  const [anchor, setAnchor] = useState<T | { x: number; y: number } | null>(null)
  return {
    anchor,
    open: anchor != null,
    openAt: (a: T | { x: number; y: number }) => setAnchor(a),
    toggle: (a: T) => setAnchor((cur) => (cur ? null : a)),
    close: () => setAnchor(null),
  }
}

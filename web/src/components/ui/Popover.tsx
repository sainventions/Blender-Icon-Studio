// Positioned floating panel rendered in a portal. Flips to stay on screen, closes on outside press / Escape.
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { cn } from '../../lib/format'

export type Placement =
  | 'bottom-start'
  | 'bottom'
  | 'bottom-end'
  | 'top-start'
  | 'top'
  | 'top-end'
  | 'right-start'
  | 'left-start'

export type Anchor = HTMLElement | { x: number; y: number } | null

export interface PopoverProps {
  open: boolean
  onClose: () => void
  anchor: Anchor
  placement?: Placement
  offset?: number
  className?: string
  style?: CSSProperties
  children: ReactNode
  /** Match the anchor's width (select menus). */
  matchWidth?: boolean
  /** Element(s) whose presses should not count as "outside" (besides the anchor). */
  ignore?: (HTMLElement | null)[]
  role?: string
  autoFocus?: boolean
}

function anchorRect(a: Anchor): DOMRect {
  if (!a) return new DOMRect(0, 0, 0, 0)
  if (a instanceof HTMLElement) return a.getBoundingClientRect()
  return new DOMRect(a.x, a.y, 0, 0)
}

export function Popover({
  open,
  onClose,
  anchor,
  placement = 'bottom-start',
  offset = 6,
  className,
  style,
  children,
  matchWidth,
  ignore,
  role = 'dialog',
  autoFocus = false,
}: PopoverProps) {
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ x: number; y: number; minW?: number; origin: string } | null>(null)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const depthRef = useRef(0)
  const wasOpen = useRef(false)

  const place = () => {
    const el = ref.current
    if (!el || !anchor) return
    const r = anchorRect(anchor)
    const w = el.offsetWidth
    const h = el.offsetHeight
    const vw = window.innerWidth
    const vh = window.innerHeight
    const m = 8
    let [side, align] = placement.split('-') as [string, string | undefined]
    let x = 0
    let y = 0
    const horizontal = side === 'left' || side === 'right'
    if (!horizontal) {
      if (side === 'bottom' && r.bottom + offset + h > vh - m && r.top - offset - h > m) side = 'top'
      else if (side === 'top' && r.top - offset - h < m && r.bottom + offset + h < vh - m) side = 'bottom'
      y = side === 'bottom' ? r.bottom + offset : r.top - offset - h
      x = align === 'start' ? r.left : align === 'end' ? r.right - w : r.left + r.width / 2 - w / 2
    } else {
      if (side === 'right' && r.right + offset + w > vw - m) side = 'left'
      else if (side === 'left' && r.left - offset - w < m) side = 'right'
      x = side === 'right' ? r.right + offset : r.left - offset - w
      y = r.top - 4
    }
    x = Math.max(m, Math.min(vw - w - m, x))
    y = Math.max(m, Math.min(vh - h - m, y))
    const origin = horizontal ? (side === 'right' ? 'left top' : 'right top') : side === 'bottom' ? 'top' : 'bottom'
    setPos({ x, y, minW: matchWidth ? r.width : undefined, origin })
  }

  useLayoutEffect(() => {
    if (!open) {
      setPos(null)
      return
    }
    place()
    const el = ref.current
    const ro = el ? new ResizeObserver(() => place()) : null
    if (el && ro) ro.observe(el)
    const onWin = () => place()
    window.addEventListener('resize', onWin)
    window.addEventListener('scroll', onWin, true)
    return () => {
      ro?.disconnect()
      window.removeEventListener('resize', onWin)
      window.removeEventListener('scroll', onWin, true)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, anchor, placement])

  useEffect(() => {
    if (!open) return
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node
      if (ref.current?.contains(t)) return
      if (anchor instanceof HTMLElement && anchor.contains(t)) return
      if (ignore?.some((el) => el?.contains(t))) return
      // Presses inside a nested popover (portaled) belong to it.
      if (t instanceof Element && t.closest('[data-popover]') && !ref.current?.contains(t)) {
        const other = t.closest('[data-popover]') as HTMLElement
        if (Number(other.dataset.depth ?? 0) > Number(ref.current?.dataset.depth ?? 0)) return
      }
      onCloseRef.current()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      // Only the innermost open popover closes (all of them listen on document).
      const nested = [...document.querySelectorAll<HTMLElement>('[data-popover]')].some((el) => Number(el.dataset.depth ?? 0) > depthRef.current)
      if (nested) return
      e.stopPropagation()
      onCloseRef.current()
    }
    document.addEventListener('pointerdown', onDown, true)
    document.addEventListener('keydown', onKey, true)
    if (autoFocus) requestAnimationFrame(() => ref.current?.focus())
    return () => {
      document.removeEventListener('pointerdown', onDown, true)
      document.removeEventListener('keydown', onKey, true)
    }
  }, [open, anchor, ignore, autoFocus])

  // Nesting depth, fixed when the popover opens (counting on every render would include this popover itself
  // and any later-opened children, so a parent could out-rank its child and close on presses inside it).
  if (open && !wasOpen.current) depthRef.current = document.querySelectorAll('[data-popover]').length
  wasOpen.current = open
  if (!open) return null
  const depth = depthRef.current
  return createPortal(
    <div
      ref={ref}
      data-popover=""
      data-depth={depth}
      role={role}
      tabIndex={-1}
      className={cn('fixed z-[900] rounded-lg glass-strong shadow-pop outline-none', pos && 'animate-pop-in', className)}
      style={{
        left: pos?.x ?? -9999,
        top: pos?.y ?? -9999,
        minWidth: pos?.minW,
        transformOrigin: pos?.origin,
        visibility: pos ? 'visible' : 'hidden',
        ...style,
      }}
    >
      {children}
    </div>,
    document.body,
  )
}

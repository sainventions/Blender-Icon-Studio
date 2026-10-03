// Global tooltip layer. Any element with `data-tip="Label"` (optional `data-tip-kbd="Mod+Z"`,
// `data-tip-side="top|bottom|left|right"`) gets a tooltip — no wrapper components needed.
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { prettyShortcut } from '../../lib/format'

interface TipState {
  label: string
  kbd?: string
  rect: DOMRect
  side: 'top' | 'bottom' | 'left' | 'right' | 'auto'
}

export function tip(label: string, kbd?: string, side?: TipState['side']): Record<string, string> {
  const a: Record<string, string> = { 'data-tip': label, 'aria-label': label }
  if (kbd) a['data-tip-kbd'] = kbd
  if (side) a['data-tip-side'] = side
  return a
}

export function TooltipLayer() {
  const [state, setState] = useState<TipState | null>(null)
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null)

  useEffect(() => {
    let showTimer: number | undefined
    let coolTimer: number | undefined
    let warm = false
    let current: HTMLElement | null = null

    const find = (t: EventTarget | null) => (t instanceof Element ? t.closest<HTMLElement>('[data-tip]') : null)
    const hide = () => {
      window.clearTimeout(showTimer)
      current = null
      setState(null)
    }
    const show = (el: HTMLElement) => {
      if (!el.isConnected || !el.dataset.tip) return
      setState({
        label: el.dataset.tip,
        kbd: el.dataset.tipKbd,
        rect: el.getBoundingClientRect(),
        side: (el.dataset.tipSide as TipState['side']) ?? 'auto',
      })
      warm = true
      window.clearTimeout(coolTimer)
    }
    const onOver = (e: PointerEvent) => {
      if (e.pointerType === 'touch') return
      const el = find(e.target)
      if (el === current) return
      current = el
      window.clearTimeout(showTimer)
      if (!el) {
        setState(null)
        coolTimer = window.setTimeout(() => (warm = false), 400)
        return
      }
      if (el.closest('[data-tip-disabled]')) return
      showTimer = window.setTimeout(() => show(el), warm ? 50 : 520)
    }
    const onOut = (e: PointerEvent) => {
      if (!e.relatedTarget) hide()
    }
    const onFocus = (e: FocusEvent) => {
      const el = find(e.target)
      if (el && el instanceof HTMLElement && el.matches(':focus-visible')) {
        current = el
        window.clearTimeout(showTimer)
        showTimer = window.setTimeout(() => show(el), 300)
      }
    }
    document.addEventListener('pointerover', onOver)
    document.addEventListener('pointerout', onOut)
    document.addEventListener('pointerdown', hide, true)
    document.addEventListener('keydown', hide, true)
    document.addEventListener('focusin', onFocus)
    document.addEventListener('focusout', hide)
    window.addEventListener('scroll', hide, true)
    window.addEventListener('blur', hide)
    return () => {
      document.removeEventListener('pointerover', onOver)
      document.removeEventListener('pointerout', onOut)
      document.removeEventListener('pointerdown', hide, true)
      document.removeEventListener('keydown', hide, true)
      document.removeEventListener('focusin', onFocus)
      document.removeEventListener('focusout', hide)
      window.removeEventListener('scroll', hide, true)
      window.removeEventListener('blur', hide)
      window.clearTimeout(showTimer)
      window.clearTimeout(coolTimer)
    }
  }, [])

  useLayoutEffect(() => {
    if (!state || !ref.current) {
      setPos(null)
      return
    }
    const r = state.rect
    const el = ref.current
    const w = el.offsetWidth
    const h = el.offsetHeight
    const vw = window.innerWidth
    const vh = window.innerHeight
    const gap = 7
    let side = state.side
    if (side === 'auto') side = r.bottom + gap + h < vh - 8 ? 'bottom' : 'top'
    let x = 0
    let y = 0
    if (side === 'bottom' || side === 'top') {
      x = r.left + r.width / 2 - w / 2
      y = side === 'bottom' ? r.bottom + gap : r.top - gap - h
      if (side === 'bottom' && y + h > vh - 4) y = r.top - gap - h
      if (side === 'top' && y < 4) y = r.bottom + gap
    } else {
      y = r.top + r.height / 2 - h / 2
      x = side === 'right' ? r.right + gap : r.left - gap - w
    }
    x = Math.max(6, Math.min(vw - w - 6, x))
    y = Math.max(6, Math.min(vh - h - 6, y))
    setPos({ x, y })
  }, [state])

  if (!state) return null
  return createPortal(
    <div
      ref={ref}
      role="tooltip"
      className="pointer-events-none fixed z-[1000] flex max-w-[280px] items-center gap-2 rounded-md px-2 py-[5px] text-2xs font-medium text-fg glass-strong shadow-pop animate-pop-in"
      style={{ left: pos?.x ?? -9999, top: pos?.y ?? -9999, visibility: pos ? 'visible' : 'hidden' }}
    >
      <span className="leading-snug">{state.label}</span>
      {state.kbd && (
        <span className="flex items-center gap-0.5">
          {state.kbd.split(' ').map((k) => (
            <kbd
              key={k}
              className="rounded-[4px] border border-line-2 bg-white/5 px-1 font-sans text-3xs font-medium text-fg-2"
            >
              {prettyShortcut(k)}
            </kbd>
          ))}
        </span>
      )}
    </div>,
    document.body,
  )
}

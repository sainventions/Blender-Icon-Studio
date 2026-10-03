// Modal dialog: blurred backdrop, pop-in panel, Escape to close, initial focus.
import { useEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { cn } from '../../lib/format'

export interface DialogProps {
  open: boolean
  onClose: () => void
  title: ReactNode
  description?: ReactNode
  icon?: ReactNode
  children: ReactNode
  footer?: ReactNode
  width?: number
  className?: string
  bodyClassName?: string
}

export function Dialog({ open, onClose, title, description, icon, children, footer, width = 560, className, bodyClassName }: DialogProps) {
  const panelRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef(onClose)
  closeRef.current = onClose

  useEffect(() => {
    if (!open) return
    const prev = document.activeElement as HTMLElement | null
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !document.querySelector('[data-popover]')) {
        e.preventDefault()
        closeRef.current()
      }
    }
    document.addEventListener('keydown', onKey)
    requestAnimationFrame(() => {
      const first = panelRef.current?.querySelector<HTMLElement>('[data-autofocus]') ?? panelRef.current
      first?.focus()
    })
    return () => {
      document.removeEventListener('keydown', onKey)
      prev?.focus?.()
    }
  }, [open])

  if (!open) return null
  return createPortal(
    <div
      className="fixed inset-0 z-[800] flex items-center justify-center bg-black/55 p-6 backdrop-blur-[6px] animate-fade-in"
      onPointerDown={(e) => {
        if (e.target === e.currentTarget) closeRef.current()
      }}
      data-dialog=""
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        tabIndex={-1}
        className={cn(
          'relative flex max-h-[min(88vh,820px)] w-full flex-col overflow-hidden rounded-2xl border border-line-2 bg-surface-1/95 shadow-[0_40px_120px_-20px_rgb(0_0_0/0.9),0_0_0_1px_rgb(255_255_255/0.04)] outline-none animate-pop-in',
          className,
        )}
        style={{ maxWidth: width }}
      >
        <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-white/25 to-transparent" />
        <header className="flex items-start gap-3 border-b border-line px-5 pb-4 pt-4">
          {icon && (
            <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg accent-gradient-soft text-fg ring-1 ring-inset ring-white/10 [&>svg]:h-4 [&>svg]:w-4">
              {icon}
            </div>
          )}
          <div className="min-w-0 flex-1">
            <h2 className="text-[14px] font-semibold tracking-[-0.01em] text-fg">{title}</h2>
            {description && <p className="mt-0.5 text-xs text-fg-3">{description}</p>}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="-mr-1 flex h-7 w-7 items-center justify-center rounded-md text-fg-3 transition-colors hover:bg-white/[0.07] hover:text-fg"
          >
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className={cn('min-h-0 flex-1 overflow-y-auto px-5 py-4', bodyClassName)}>{children}</div>
        {footer && <footer className="flex items-center gap-2 border-t border-line bg-surface-0/60 px-5 py-3">{footer}</footer>}
      </div>
    </div>,
    document.body,
  )
}

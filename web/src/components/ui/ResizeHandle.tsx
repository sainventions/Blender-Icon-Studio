// Vertical drag handle for resizable side panels.
import { useRef, useState } from 'react'
import { cn } from '../../lib/format'

export function ResizeHandle({
  side,
  onResize,
  onReset,
  className,
}: {
  /** Which panel edge this handle sits on: 'left' panel → handle on its right edge. */
  side: 'left' | 'right'
  onResize: (delta: number, phase: 'start' | 'move' | 'end') => void
  onReset?: () => void
  className?: string
}) {
  const start = useRef<number | null>(null)
  const [active, setActive] = useState(false)
  return (
    <div
      role="separator"
      aria-orientation="vertical"
      onPointerDown={(e) => {
        e.preventDefault()
        ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
        start.current = e.clientX
        setActive(true)
        onResize(0, 'start')
      }}
      onPointerMove={(e) => {
        if (start.current == null) return
        const d = e.clientX - start.current
        onResize(side === 'left' ? d : -d, 'move')
      }}
      onPointerUp={() => {
        if (start.current == null) return
        start.current = null
        setActive(false)
        onResize(0, 'end')
      }}
      onPointerCancel={() => {
        start.current = null
        setActive(false)
      }}
      onDoubleClick={onReset}
      data-tip="Drag to resize · double-click to reset"
      data-tip-side={side === 'left' ? 'right' : 'left'}
      className={cn(
        'group absolute inset-y-0 z-20 w-[7px] cursor-col-resize',
        side === 'left' ? '-right-[4px]' : '-left-[4px]',
        className,
      )}
    >
      <div
        className={cn(
          'absolute inset-y-0 left-[3px] w-px transition-colors duration-150',
          active ? 'bg-accent' : 'bg-transparent group-hover:bg-accent/60',
        )}
      />
    </div>
  )
}

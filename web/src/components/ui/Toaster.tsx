import { CircleAlert, CircleCheck, Download, Info, TriangleAlert, X } from 'lucide-react'
import { createPortal } from 'react-dom'
import { cn } from '../../lib/format'
import { useToasts, type Toast } from '../../store/toasts'
import { ProgressBar, ProgressRing } from './misc'

function ToastIcon({ t }: { t: Toast }) {
  switch (t.kind) {
    case 'success':
      return <CircleCheck className="h-4 w-4 text-ok" />
    case 'error':
      return <CircleAlert className="h-4 w-4 text-bad" />
    case 'warning':
      return <TriangleAlert className="h-4 w-4 text-warn" />
    case 'progress':
      return <ProgressRing value={t.progress ?? null} size={16} stroke={2} />
    default:
      return <Info className="h-4 w-4 text-info" />
  }
}

export function Toaster() {
  const toasts = useToasts((s) => s.toasts)
  const dismiss = useToasts((s) => s.dismiss)
  return createPortal(
    <div className="pointer-events-none fixed bottom-9 right-3 z-[950] flex w-[340px] flex-col gap-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          role={t.kind === 'error' ? 'alert' : 'status'}
          className={cn(
            'pointer-events-auto relative overflow-hidden rounded-xl glass-strong shadow-pop animate-slide-up',
            t.kind === 'error' && 'border-bad/30',
          )}
        >
          <div className="flex items-start gap-2.5 px-3 py-2.5">
            <div className="mt-[1px] shrink-0">
              <ToastIcon t={t} />
            </div>
            <div className="min-w-0 flex-1">
              <div className="text-xs font-semibold text-fg">{t.title}</div>
              {t.description && <div className="mt-0.5 break-words text-2xs leading-relaxed text-fg-3">{t.description}</div>}
              {(!!t.action || !!t.links?.length) && (
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                  {t.action && (
                    <button
                      type="button"
                      onClick={() => {
                        t.action!.onClick()
                        if (t.kind !== 'progress') dismiss(t.id)
                      }}
                      className="h-6 rounded-md bg-white/[0.08] px-2 text-2xs font-semibold text-fg transition-colors hover:bg-white/[0.14]"
                    >
                      {t.action.label}
                    </button>
                  )}
                  {t.links?.map((l) => (
                    <a
                      key={l.href}
                      href={l.href}
                      download={l.download ? '' : undefined}
                      target={l.download ? undefined : '_blank'}
                      rel="noreferrer"
                      className="inline-flex h-6 items-center gap-1 rounded-md bg-white/[0.08] px-2 text-2xs font-semibold text-fg transition-colors hover:bg-white/[0.14]"
                    >
                      {l.download && <Download className="h-3 w-3" />}
                      {l.label}
                    </a>
                  ))}
                </div>
              )}
            </div>
            <button
              type="button"
              onClick={() => dismiss(t.id)}
              aria-label="Dismiss"
              className="-mr-1 -mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded text-fg-4 transition-colors hover:bg-white/10 hover:text-fg"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
          {t.kind === 'progress' && <ProgressBar value={t.progress ?? null} className="absolute inset-x-0 bottom-0 h-[2px] rounded-none" />}
        </div>
      ))}
    </div>,
    document.body,
  )
}

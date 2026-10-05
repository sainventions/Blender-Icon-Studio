// Promise-based confirmation dialog: `if (await confirmDialog({...})) …`, the in-app replacement for window.confirm.
import type { ReactNode } from 'react'
import { create } from 'zustand'
import { TriangleAlert } from 'lucide-react'
import { Button } from './Button'
import { Dialog } from './Dialog'

export interface ConfirmOptions {
  title: ReactNode
  description?: ReactNode
  confirmLabel?: string
  cancelLabel?: string
  danger?: boolean
  icon?: ReactNode
}

interface Pending extends ConfirmOptions {
  resolve: (ok: boolean) => void
}

const useConfirm = create<{ pending: Pending | null }>(() => ({ pending: null }))

export function confirmDialog(opts: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    useConfirm.getState().pending?.resolve(false) // a newer question supersedes an unanswered one
    useConfirm.setState({ pending: { ...opts, resolve } })
  })
}

function settle(ok: boolean) {
  const p = useConfirm.getState().pending
  if (!p) return
  useConfirm.setState({ pending: null })
  p.resolve(ok)
}

/** Mount once (App). */
export function ConfirmHost() {
  const pending = useConfirm((s) => s.pending)
  return (
    <Dialog
      open={!!pending}
      onClose={() => settle(false)}
      width={420}
      icon={pending?.icon ?? (pending?.danger ? <TriangleAlert /> : undefined)}
      title={pending?.title ?? ''}
      footer={
        <>
          <div className="flex-1" />
          <Button variant="ghost" onClick={() => settle(false)}>
            {pending?.cancelLabel ?? 'Cancel'}
          </Button>
          <Button variant={pending?.danger ? 'danger' : 'primary'} onClick={() => settle(true)} data-autofocus>
            {pending?.confirmLabel ?? 'OK'}
          </Button>
        </>
      }
    >
      {pending?.description && <div className="text-xs leading-relaxed text-fg-3">{pending.description}</div>}
    </Dialog>
  )
}

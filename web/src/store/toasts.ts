// Toast notifications (bottom-right stack). Use the `toast` helpers from anywhere; no React needed.
import { create } from 'zustand'

export type ToastKind = 'info' | 'success' | 'error' | 'warning' | 'progress'

export interface ToastLink {
  label: string
  href: string
  download?: boolean
}

export interface Toast {
  id: string
  kind: ToastKind
  title: string
  description?: string
  /** 0..1 for determinate progress, null = indeterminate (kind 'progress'). */
  progress?: number | null
  action?: { label: string; onClick: () => void }
  links?: ToastLink[]
  /** ms until auto-dismiss; null = sticky. */
  duration?: number | null
  createdAt: number
}

interface ToastState {
  toasts: Toast[]
  push: (t: Omit<Toast, 'id' | 'createdAt'> & { id?: string }) => string
  update: (id: string, patch: Partial<Omit<Toast, 'id'>>) => void
  dismiss: (id: string) => void
}

let seq = 0
const timers = new Map<string, number>()

function arm(id: string, duration: number | null | undefined, kind: ToastKind) {
  const prev = timers.get(id)
  if (prev) window.clearTimeout(prev)
  timers.delete(id)
  const ms = duration === undefined ? (kind === 'error' ? 8000 : kind === 'progress' ? null : 4200) : duration
  if (ms == null) return
  timers.set(
    id,
    window.setTimeout(() => useToasts.getState().dismiss(id), ms),
  )
}

export const useToasts = create<ToastState>((set, get) => ({
  toasts: [],
  push: (t) => {
    const id = t.id ?? `t${++seq}`
    const existing = get().toasts.find((x) => x.id === id)
    const toast: Toast = { ...t, id, createdAt: existing?.createdAt ?? Date.now() }
    set((s) => ({
      toasts: existing ? s.toasts.map((x) => (x.id === id ? toast : x)) : [...s.toasts, toast].slice(-6),
    }))
    arm(id, t.duration, t.kind)
    return id
  },
  update: (id, patch) => {
    const cur = get().toasts.find((x) => x.id === id)
    if (!cur) return
    const next = { ...cur, ...patch }
    set((s) => ({ toasts: s.toasts.map((x) => (x.id === id ? next : x)) }))
    if (patch.kind || patch.duration !== undefined) arm(id, next.duration, next.kind)
  },
  dismiss: (id) => {
    const t = timers.get(id)
    if (t) window.clearTimeout(t)
    timers.delete(id)
    set((s) => ({ toasts: s.toasts.filter((x) => x.id !== id) }))
  },
}))

type Opts = Partial<Omit<Toast, 'id' | 'kind' | 'title' | 'createdAt'>> & { id?: string }

export const toast = {
  info: (title: string, o: Opts = {}) => useToasts.getState().push({ kind: 'info', title, ...o }),
  success: (title: string, o: Opts = {}) => useToasts.getState().push({ kind: 'success', title, ...o }),
  warning: (title: string, o: Opts = {}) => useToasts.getState().push({ kind: 'warning', title, ...o }),
  error: (title: string, o: Opts = {}) => useToasts.getState().push({ kind: 'error', title, ...o }),
  progress: (title: string, o: Opts = {}) =>
    useToasts.getState().push({ kind: 'progress', title, progress: null, duration: null, ...o }),
  update: (id: string, patch: Partial<Omit<Toast, 'id'>>) => useToasts.getState().update(id, patch),
  dismiss: (id: string) => useToasts.getState().dismiss(id),
}

// Minimal hash router: "#/" → home, "#/p/<projectId>" → editor, "#/pack" → Icon Pack.
import { useSyncExternalStore } from 'react'

export type Route = { name: 'home' } | { name: 'editor'; projectId: string } | { name: 'pack' }

function parse(hash: string): Route {
  const m = hash.match(/^#\/p\/([^/?#]+)/)
  if (m) return { name: 'editor', projectId: decodeURIComponent(m[1]) }
  if (/^#\/pack(?:[/?]|$)/.test(hash)) return { name: 'pack' }
  return { name: 'home' }
}

let current: Route = parse(typeof window !== 'undefined' ? window.location.hash : '')
const listeners = new Set<() => void>()

if (typeof window !== 'undefined') {
  window.addEventListener('hashchange', () => {
    current = parse(window.location.hash)
    listeners.forEach((l) => l())
  })
}

export function useRoute(): Route {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb)
      return () => listeners.delete(cb)
    },
    () => current,
  )
}

export function navigate(to: Route) {
  const hash = to.name === 'editor' ? `#/p/${encodeURIComponent(to.projectId)}` : to.name === 'pack' ? '#/pack' : '#/'
  if (window.location.hash !== hash) window.location.hash = hash
}

export const openProjectRoute = (projectId: string) => navigate({ name: 'editor', projectId })
export const goHome = () => navigate({ name: 'home' })
export const goPack = () => navigate({ name: 'pack' })

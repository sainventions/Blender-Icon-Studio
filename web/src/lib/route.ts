// Minimal hash router: "#/" → home, "#/p/<projectId>" → editor.
import { useSyncExternalStore } from 'react'

export type Route = { name: 'home' } | { name: 'editor'; projectId: string }

function parse(hash: string): Route {
  const m = hash.match(/^#\/p\/([^/?#]+)/)
  if (m) return { name: 'editor', projectId: decodeURIComponent(m[1]) }
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
  const hash = to.name === 'editor' ? `#/p/${encodeURIComponent(to.projectId)}` : '#/'
  if (window.location.hash !== hash) window.location.hash = hash
}

export const openProjectRoute = (projectId: string) => navigate({ name: 'editor', projectId })
export const goHome = () => navigate({ name: 'home' })

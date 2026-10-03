import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefCallback } from 'react'

/** Stable callback that always sees the latest props/state (like the proposed useEffectEvent). */
export function useEvent<A extends unknown[], R>(fn: (...args: A) => R): (...args: A) => R {
  const ref = useRef(fn)
  useLayoutEffect(() => {
    ref.current = fn
  })
  return useCallback((...args: A) => ref.current(...args), [])
}

/** Re-render every `interval` ms while `active`. Returns Date.now() of the last tick. */
export function useNow(active: boolean, interval = 100): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    setNow(Date.now())
    const t = window.setInterval(() => setNow(Date.now()), interval)
    return () => window.clearInterval(t)
  }, [active, interval])
  return now
}

/** Observe an element's content-box size. */
export function useElementSize<T extends HTMLElement>(): [RefCallback<T>, { width: number; height: number }] {
  const [size, setSize] = useState({ width: 0, height: 0 })
  const roRef = useRef<ResizeObserver | null>(null)
  const ref = useCallback((el: T | null) => {
    roRef.current?.disconnect()
    roRef.current = null
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect
      if (r) setSize((s) => (s.width === r.width && s.height === r.height ? s : { width: r.width, height: r.height }))
    })
    ro.observe(el)
    roRef.current = ro
  }, [])
  return [ref, size]
}

/** Debounced value. */
export function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = window.setTimeout(() => setV(value), ms)
    return () => window.clearTimeout(t)
  }, [value, ms])
  return v
}

/** Smoothly animate a number toward a target (rAF, critically damped-ish easing). */
export function useAnimatedNumber(target: number, onFrame: (v: number) => void, durationMs = 420) {
  const fromRef = useRef(target)
  const lastRef = useRef(target)
  const cb = useEvent(onFrame)
  useEffect(() => {
    const from = lastRef.current
    fromRef.current = from
    if (from === target) return
    const t0 = performance.now()
    let raf = 0
    const step = (t: number) => {
      const k = Math.min(1, (t - t0) / durationMs)
      const e = 1 - Math.pow(1 - k, 3)
      const v = from + (target - from) * e
      lastRef.current = v
      cb(v)
      if (k < 1) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [target, durationMs, cb])
}

/** True when an event target is a text-entry element (shortcuts must not fire). */
export function isTypingTarget(t: EventTarget | null): boolean {
  if (!(t instanceof HTMLElement)) return false
  if (t.isContentEditable) return true
  const tag = t.tagName
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true
  if (tag === 'INPUT') {
    const type = (t as HTMLInputElement).type
    return !['checkbox', 'radio', 'range', 'button', 'submit', 'color'].includes(type)
  }
  return false
}

/** Safe localStorage access (private windows / blocked storage). */
export const safeStorage = {
  get(key: string): string | null {
    try {
      return window.localStorage.getItem(key)
    } catch {
      return null
    }
  },
  set(key: string, value: string) {
    try {
      window.localStorage.setItem(key, value)
    } catch {
      /* ignore */
    }
  },
  remove(key: string) {
    try {
      window.localStorage.removeItem(key)
    } catch {
      /* ignore */
    }
  },
}

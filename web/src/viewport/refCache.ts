// Reference-counted GPU resource cache that is safe under React StrictMode.
//
// `get()` may be called during render (get-or-create, no ref taken); mounted users `retain()`/`release()` in
// effects. Entries that end up with zero refs (released, or created by a render that never committed) are
// destroyed after a short grace period — so switching projects back and forth reuses work, but nothing leaks.
import { useEffect, useMemo } from 'react'

interface Entry<V> {
  value: V
  refs: number
  timer: ReturnType<typeof setTimeout> | null
}

export class RefCache<K extends string, V> {
  private entries = new Map<K, Entry<V>>()

  constructor(
    private readonly destroy: (value: V, key: K) => void,
    private readonly graceMs = 1500,
  ) {}

  get size(): number {
    return this.entries.size
  }

  has(key: K): boolean {
    return this.entries.has(key)
  }

  peek(key: K): V | undefined {
    return this.entries.get(key)?.value
  }

  get(key: K, create: () => V): V {
    let e = this.entries.get(key)
    if (!e) {
      e = { value: create(), refs: 0, timer: null }
      this.entries.set(key, e)
      this.schedule(key, e)
    }
    return e.value
  }

  retain(key: K): void {
    const e = this.entries.get(key)
    if (!e) return
    e.refs++
    if (e.timer) {
      clearTimeout(e.timer)
      e.timer = null
    }
  }

  release(key: K): void {
    const e = this.entries.get(key)
    if (!e) return
    e.refs = Math.max(0, e.refs - 1)
    if (e.refs === 0) this.schedule(key, e)
  }

  /** Destroy everything (e.g. when the WebGL context goes away). */
  clear(): void {
    for (const [k, e] of this.entries) {
      if (e.timer) clearTimeout(e.timer)
      this.destroy(e.value, k)
    }
    this.entries.clear()
  }

  private schedule(key: K, e: Entry<V>): void {
    if (e.timer) clearTimeout(e.timer)
    e.timer = setTimeout(() => {
      e.timer = null
      if (e.refs === 0 && this.entries.get(key) === e) {
        this.entries.delete(key)
        this.destroy(e.value, key)
      }
    }, this.graceMs)
  }
}

/** Get-or-create `key` from `cache` and hold a reference for as long as the component uses it. */
export function useCached<K extends string, V>(cache: RefCache<K, V>, key: K | null, create: () => V): V | null {
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const value = useMemo(() => (key === null ? null : cache.get(key, create)), [cache, key])
  useEffect(() => {
    if (key === null) return
    // The entry may have been swept between render and commit (very slow commits): recreate it.
    cache.get(key, create)
    cache.retain(key)
    return () => cache.release(key)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cache, key])
  return value
}

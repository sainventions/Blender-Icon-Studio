// WebSocket client for /ws with automatic reconnect (exponential backoff + jitter).
import type { WsEvent } from '../types'

export type WsState = 'connecting' | 'open' | 'closed'

export interface WsClient {
  close(): void
}

export function connectEvents(handlers: {
  onEvent: (e: WsEvent) => void
  onState?: (s: WsState) => void
  /** Called after every (re)connect so the app can resync state it may have missed. */
  onOpen?: (reconnect: boolean) => void
}): WsClient {
  let ws: WebSocket | null = null
  let closed = false
  let attempt = 0
  let timer: number | undefined
  let everOpened = false

  const url = () => `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws`

  const schedule = () => {
    if (closed) return
    const delay = Math.min(8000, 400 * 2 ** attempt) * (0.75 + Math.random() * 0.5)
    attempt++
    timer = window.setTimeout(open, delay)
  }

  function open() {
    if (closed) return
    handlers.onState?.('connecting')
    try {
      ws = new WebSocket(url())
    } catch {
      handlers.onState?.('closed')
      schedule()
      return
    }
    ws.onopen = () => {
      attempt = 0
      handlers.onState?.('open')
      handlers.onOpen?.(everOpened)
      everOpened = true
    }
    ws.onmessage = (msg) => {
      if (typeof msg.data !== 'string') return
      try {
        const data = JSON.parse(msg.data) as WsEvent
        if (data && typeof data === 'object' && 'type' in data) handlers.onEvent(data)
      } catch {
        /* ignore malformed frames */
      }
    }
    ws.onclose = () => {
      ws = null
      handlers.onState?.('closed')
      schedule()
    }
    ws.onerror = () => {
      try {
        ws?.close()
      } catch {
        /* ignore */
      }
    }
  }

  open()

  return {
    close() {
      closed = true
      window.clearTimeout(timer)
      if (ws) {
        ws.onclose = null
        ws.close()
      }
    },
  }
}

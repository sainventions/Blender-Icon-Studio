// Thin fetch wrapper for the FastAPI backend (/api/*). JSON in, JSON out, typed errors.

export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown
  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
  /** True when the backend could not be reached at all (server down / proxy error). */
  get offline(): boolean {
    return this.status === 0 || this.status === 502 || this.status === 503 || this.status === 504
  }
}

export const API_PREFIX = '/api'

export function apiUrl(path: string): string {
  return path.startsWith('/api') ? path : `${API_PREFIX}${path.startsWith('/') ? '' : '/'}${path}`
}

function messageFromDetail(detail: unknown, fallback: string): string {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    // FastAPI validation errors: [{loc, msg, type}]
    const msgs = detail
      .map((d) => (d && typeof d === 'object' && 'msg' in d ? String((d as { msg: unknown }).msg) : null))
      .filter(Boolean)
    if (msgs.length) return msgs.join('; ')
  }
  if (detail && typeof detail === 'object' && 'message' in detail) return String((detail as { message: unknown }).message)
  return fallback
}

export interface RequestOptions {
  signal?: AbortSignal
  keepalive?: boolean
  /** Raw body (FormData) — sent without a JSON content-type. */
  form?: FormData
}

export async function request<T>(method: string, path: string, body?: unknown, opts: RequestOptions = {}): Promise<T> {
  const init: RequestInit = { method, signal: opts.signal, keepalive: opts.keepalive, headers: {} }
  if (opts.form) {
    init.body = opts.form
  } else if (body !== undefined) {
    init.body = JSON.stringify(body)
    ;(init.headers as Record<string, string>)['Content-Type'] = 'application/json'
  }
  let res: Response
  try {
    res = await fetch(apiUrl(path), init)
  } catch (e) {
    if ((e as Error)?.name === 'AbortError') throw e
    throw new ApiError(0, 'Cannot reach the Blender Icon Studio server (port 8420).')
  }
  const text = await res.text()
  let data: unknown = undefined
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = text
    }
  }
  if (!res.ok) {
    const detail = data && typeof data === 'object' && 'detail' in data ? (data as { detail: unknown }).detail : data
    const fallback =
      res.status === 404
        ? 'Not found'
        : res.status >= 500
          ? `Server error (${res.status})`
          : `Request failed (${res.status})`
    throw new ApiError(res.status, messageFromDetail(detail, fallback), detail)
  }
  return data as T
}

export const http = {
  get: <T>(path: string, opts?: RequestOptions) => request<T>('GET', path, undefined, opts),
  post: <T>(path: string, body?: unknown, opts?: RequestOptions) => request<T>('POST', path, body ?? {}, opts),
  put: <T>(path: string, body?: unknown, opts?: RequestOptions) => request<T>('PUT', path, body, opts),
  del: <T>(path: string, opts?: RequestOptions) => request<T>('DELETE', path, undefined, opts),
}

export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message
  if (e instanceof Error) return e.message
  return String(e)
}

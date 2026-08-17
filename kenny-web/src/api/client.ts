/**
 * Typed fetch wrapper. Mirrors the old dashboard's `getJSON`/`api` helpers
 * (notes/api-contract-actual.md §1, §3) exactly where it matters:
 *
 * - 401 is a FULL PAGE NAVIGATION (`location.assign('/login')`), not a
 *   client route change — this deliberately drops all SPA state and is how
 *   the app has always behaved. No token refresh, no retry, no "session
 *   expiring soon" warning. Do not add any of those.
 * - Non-2xx responses are read as JSON and their `error` field, if present,
 *   becomes the thrown message.
 */

export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

function hasErrorField(data: unknown): data is { error: string } {
  return (
    typeof data === 'object' &&
    data !== null &&
    'error' in data &&
    typeof (data as Record<string, unknown>).error === 'string'
  )
}

/** Redirects to /login. Split out so callers/tests can see the seam without touching real `location`. */
export function redirectToLogin(): void {
  location.assign('/login')
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init)

  if (res.status === 401) {
    redirectToLogin()
    throw new ApiError('unauthorized', 401)
  }

  let data: unknown = null
  try {
    data = await res.json()
  } catch {
    // No body, or not JSON — fine for a 204, fatal below for a non-2xx.
  }

  if (!res.ok) {
    const message = hasErrorField(data) ? data.error : `${path} -> ${res.status}`
    throw new ApiError(message, res.status)
  }

  return data as T
}

function jsonInit(method: string, body?: unknown): RequestInit {
  if (body === undefined) return { method }
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

/** The client every view/query hook should import — never call `fetch` directly. */
export const api = {
  get: <T>(path: string): Promise<T> => apiFetch<T>(path),
  post: <T>(path: string, body?: unknown): Promise<T> => apiFetch<T>(path, jsonInit('POST', body)),
  put: <T>(path: string, body?: unknown): Promise<T> => apiFetch<T>(path, jsonInit('PUT', body)),
  patch: <T>(path: string, body?: unknown): Promise<T> => apiFetch<T>(path, jsonInit('PATCH', body)),
  delete: <T>(path: string, body?: unknown): Promise<T> => apiFetch<T>(path, jsonInit('DELETE', body)),
}

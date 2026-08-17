/**
 * Every localStorage access in the app must go through these two functions.
 * Storage can throw (private browsing, quota, disabled) — the old dashboard
 * wrapped every read/write in try/catch and so do we (notes/api-contract-
 * actual.md §4).
 */

export function safeGetItem(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

export function safeSetItem(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    // Storage unavailable — the preference simply doesn't persist this
    // session. Never let a write failure crash the app.
  }
}

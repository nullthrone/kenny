import { api } from '../api/client'
import type { ThemePreference } from '../api/types'
import type { Theme } from './theme'

/**
 * Persist the console theme against the signed-in account, best-effort.
 *
 * Deliberately fire-and-forget: localStorage has already been written and the
 * document already repainted by the time this runs, so a failure here costs the
 * operator nothing this session — it only means the choice will not follow them
 * to another browser. Surfacing an error for that would be noise on an action
 * that visibly succeeded.
 *
 * A legacy shared-token identity has no account row to store against. That is
 * not an error either: the server answers `200 {"stored": false}` for it
 * (`users.py::api_me_theme`), so this call needs no principal check of its own.
 */
export function persistThemeForAccount(theme: Theme): void {
  const body: ThemePreference = { theme }
  void api.put<{ theme: Theme; stored: boolean }>('/api/me/theme', body).catch(() => {
    // See above: localStorage is the fallback and is already written.
  })
}

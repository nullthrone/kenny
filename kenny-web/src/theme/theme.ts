import { safeGetItem, safeSetItem } from './storage'

export type Theme = 'light' | 'dark'

export const THEME_STORAGE_KEY = 'kenny-theme'

/**
 * Pure migration logic, kept separate from the provider so it is trivially
 * testable without a DOM.
 *
 * The OLD dashboard stored "dark" | "light" under this exact key and
 * defaulted to dark (an inline script set `data-theme` from it, falling
 * back to `'dark'`). The redesign defaults to LIGHT instead. The migration
 * rule: an existing EXPLICIT stored value is honoured either way; only the
 * absence of a value resolves to the new default (light). A returning
 * operator who chose dark keeps dark — we never silently flip them.
 */
export function resolveInitialTheme(stored: string | null): Theme {
  if (stored === 'dark') return 'dark'
  if (stored === 'light') return 'light'
  return 'light'
}

export function readStoredTheme(): Theme {
  return resolveInitialTheme(safeGetItem(THEME_STORAGE_KEY))
}

export function persistTheme(theme: Theme): void {
  safeSetItem(THEME_STORAGE_KEY, theme)
}

export function applyThemeToDocument(theme: Theme): void {
  document.documentElement.setAttribute('data-theme', theme)
}

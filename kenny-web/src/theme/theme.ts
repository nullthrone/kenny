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

const THEME_FADE_CLASS = 'kc-theme-fade'
const THEME_FADE_MS = 360
let themeFadeTimer: ReturnType<typeof setTimeout> | undefined

/**
 * Cross-fade the next theme repaint instead of snapping to it.
 *
 * Only for a switch the operator makes: adopting the account's stored theme on
 * load must not animate. The class carries the transitions (global.css,
 * `.kc-theme-fade`) and is dropped again once they have run, so it never slows
 * ordinary hover feedback. Reduced motion is honoured in the stylesheet.
 */
export function fadeThemeChange(): void {
  const root = document.documentElement
  root.classList.add(THEME_FADE_CLASS)
  clearTimeout(themeFadeTimer)
  themeFadeTimer = setTimeout(() => root.classList.remove(THEME_FADE_CLASS), THEME_FADE_MS)
}

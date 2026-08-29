import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'
import { persistThemeForAccount } from './api'
import { applyThemeToDocument, persistTheme, readStoredTheme, type Theme } from './theme'

interface ThemeContextValue {
  theme: Theme
  /** Flip the theme on the operator's behalf: repaint, store locally, persist to the account. */
  toggleTheme: () => void
  /** Same, for a specific value. */
  setTheme: (theme: Theme) => void
  /**
   * Take on a theme the account already holds, WITHOUT writing it back.
   *
   * This is how `GET /api/me`'s stored `theme` reaches the document on a browser
   * that has never seen this account. It must not persist: doing so would echo the
   * server's own value straight back at it on every load, and — worse — a browser
   * that adopted a value would then be indistinguishable from one where the
   * operator chose it.
   */
  adoptTheme: (theme: Theme) => void
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

export function ThemeProvider({ children }: { children: ReactNode }) {
  // index.html's inline boot script already set `data-theme` on <html>
  // before this ever mounts (no flash of the wrong theme); this just picks
  // up the same value so React's model agrees with the DOM.
  const [theme, setThemeState] = useState<Theme>(() => readStoredTheme())

  const adoptTheme = useCallback((next: Theme) => {
    setThemeState(next)
    applyThemeToDocument(next)
    persistTheme(next)
  }, [])

  const setTheme = useCallback(
    (next: Theme) => {
      adoptTheme(next)
      persistThemeForAccount(next)
    },
    [adoptTheme],
  )

  const toggleTheme = useCallback(() => {
    setThemeState((prev) => {
      const next: Theme = prev === 'dark' ? 'light' : 'dark'
      applyThemeToDocument(next)
      persistTheme(next)
      persistThemeForAccount(next)
      return next
    })
  }, [])

  const value = useMemo(
    () => ({ theme, toggleTheme, setTheme, adoptTheme }),
    [theme, toggleTheme, setTheme, adoptTheme],
  )

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error('useTheme must be used within a ThemeProvider')
  return ctx
}

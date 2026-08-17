import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'
import { applyThemeToDocument, persistTheme, readStoredTheme, type Theme } from './theme'

interface ThemeContextValue {
  theme: Theme
  toggleTheme: () => void
  setTheme: (theme: Theme) => void
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

export function ThemeProvider({ children }: { children: ReactNode }) {
  // index.html's inline boot script already set `data-theme` on <html>
  // before this ever mounts (no flash of the wrong theme); this just picks
  // up the same value so React's model agrees with the DOM.
  const [theme, setThemeState] = useState<Theme>(() => readStoredTheme())

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next)
    applyThemeToDocument(next)
    persistTheme(next)
  }, [])

  const toggleTheme = useCallback(() => {
    setThemeState((prev) => {
      const next: Theme = prev === 'dark' ? 'light' : 'dark'
      applyThemeToDocument(next)
      persistTheme(next)
      return next
    })
  }, [])

  const value = useMemo(() => ({ theme, toggleTheme, setTheme }), [theme, toggleTheme, setTheme])

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error('useTheme must be used within a ThemeProvider')
  return ctx
}

import { describe, expect, it } from 'vitest'
import { resolveInitialTheme } from './theme'

describe('resolveInitialTheme', () => {
  it('defaults to light when nothing is stored (new default, not the old dark default)', () => {
    expect(resolveInitialTheme(null)).toBe('light')
  })

  it('honours an explicit stored "dark" — never silently flips a returning dark-mode operator', () => {
    expect(resolveInitialTheme('dark')).toBe('dark')
  })

  it('honours an explicit stored "light"', () => {
    expect(resolveInitialTheme('light')).toBe('light')
  })

  it('falls back to light for an unrecognised stored value', () => {
    expect(resolveInitialTheme('sepia')).toBe('light')
  })
})

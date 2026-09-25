import { describe, expect, it } from 'vitest'
import { formatLogTimestamp } from './format'

const NOW = new Date(2026, 8, 25, 12, 0)

describe('formatLogTimestamp', () => {
  it('shows day, month and time for rows from the current year', () => {
    const ts = new Date(2026, 8, 20, 19, 56).toISOString()
    expect(formatLogTimestamp(ts, NOW, 'de-DE')).toBe('20.09., 19:56')
  })

  it('adds the year for rows from another year', () => {
    const ts = new Date(2025, 11, 31, 23, 5).toISOString()
    expect(formatLogTimestamp(ts, NOW, 'de-DE')).toBe('31.12.2025, 23:05')
  })

  it('renders a dash for an unparseable timestamp', () => {
    expect(formatLogTimestamp('not-a-date', NOW)).toBe('—')
  })
})

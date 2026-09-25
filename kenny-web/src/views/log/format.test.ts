import { describe, expect, it } from 'vitest'
import { formatLogTimestamp } from './format'

describe('formatLogTimestamp', () => {
  it('always shows the full date, then the time, without a comma', () => {
    const ts = new Date(2026, 8, 20, 19, 56).toISOString()
    expect(formatLogTimestamp(ts, 'de-DE')).toBe('20.09.2026 19:56')
  })

  it('renders a dash for an unparseable timestamp', () => {
    expect(formatLogTimestamp('not-a-date')).toBe('—')
  })
})

import { describe, expect, it } from 'vitest'
import { hostFromHash } from './scope'

describe('hostFromHash', () => {
  it('extracts the host from a host page hash', () => {
    expect(hostFromHash('#/fleet/oma-pc')).toBe('oma-pc')
  })

  it('is empty (unscoped) for the bare fleet list', () => {
    expect(hostFromHash('#/fleet')).toBe('')
  })

  it('is empty for unrelated routes', () => {
    expect(hostFromHash('#/today')).toBe('')
    expect(hostFromHash('#/inbox/ticket/41')).toBe('')
  })

  it('decodes a percent-encoded host id', () => {
    expect(hostFromHash('#/fleet/tante-laptop%20b')).toBe('tante-laptop b')
  })
})

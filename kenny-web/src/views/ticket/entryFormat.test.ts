import { describe, expect, it } from 'vitest'
import type { TimelineEntry } from './types'
import { findingOf, verdictLabel, verdictTone } from './entryFormat'

function entry(fields: Record<string, unknown>): TimelineEntry {
  return {
    at: '2026-08-19T10:00:00Z',
    actor: 'triage',
    kind: 'finding',
    text: '',
    body: 'status',
    source_event_ids: [1],
    fields,
  }
}

const VERDICT_FIELDS = {
  verdict: 'phantom',
  finding: 'the device this names is not on this PC',
  evidence: 'diag_services lists only Harddisk0',
  resolvable: true,
}

describe('findingOf', () => {
  it('reads a verdict off a finding entry', () => {
    const found = findingOf(entry(VERDICT_FIELDS))
    expect(found).not.toBeNull()
    expect(found?.verdict).toBe('phantom')
    expect(found?.finding).toBe('the device this names is not on this PC')
    expect(found?.evidence).toBe('diag_services lists only Harddisk0')
  })

  it('is null for every entry the server did not fill fields for', () => {
    // The server fills `fields` only for a finding, and a build of this UI
    // that has never heard of some later entry kind must not mistake one for
    // a verdict — so the `verdict` field decides, never the kind.
    expect(findingOf(entry({}))).toBeNull()
    expect(findingOf({ ...entry({}), kind: 'activity' })).toBeNull()
    expect(findingOf({ ...entry({}), kind: 'message', actor: 'operator:3' })).toBeNull()
  })

  it('carries the reason the server declined to act on a verdict', () => {
    // The most informative row on the page while auto-resolve is still off:
    // it says what would have happened with it on.
    const found = findingOf(
      entry({
        ...VERDICT_FIELDS,
        resolvable: false,
        not_resolved_because: 'no read-only check actually ran',
      }),
    )
    expect(found?.notResolvedBecause).toBe('no read-only check actually ran')
  })

  it('leaves notResolvedBecause null when the verdict was acted on', () => {
    expect(findingOf(entry(VERDICT_FIELDS))?.notResolvedBecause).toBeNull()
  })

  it('reads a well-formed suppression suggestion', () => {
    const found = findingOf(
      entry({
        ...VERDICT_FIELDS,
        suppression_suggestion: { source: 'Microsoft-Windows-CAPI2', event_id: 4176 },
      }),
    )
    expect(found?.suggestion).toEqual({ source: 'Microsoft-Windows-CAPI2', event_id: 4176 })
  })

  it('drops a malformed suggestion rather than half-rendering it', () => {
    // The button it would draw creates a real rule, so a suggestion missing a
    // field must not become one with a blank in it.
    for (const bad of [
      { source: 'CAPI2' },
      { event_id: 4176 },
      { source: '', event_id: 4176 },
      { source: 'CAPI2', event_id: 'four thousand' },
      'not an object',
      null,
    ]) {
      const found = findingOf(entry({ ...VERDICT_FIELDS, suppression_suggestion: bad }))
      expect(found?.suggestion).toBeNull()
    }
  })

  it('survives fields that are absent or the wrong type', () => {
    const found = findingOf(entry({ verdict: 'inconclusive', finding: 42 }))
    expect(found?.verdict).toBe('inconclusive')
    expect(found?.finding).toBe('')
    expect(found?.evidence).toBe('')
  })
})

describe('verdictTone', () => {
  it('separates the three answers a reader acts on differently', () => {
    expect(verdictTone('phantom')).toBe('settled')
    expect(verdictTone('benign_known')).toBe('settled')
    expect(verdictTone('resolved_itself')).toBe('settled')
    expect(verdictTone('actionable')).toBe('attention')
    expect(verdictTone('inconclusive')).toBe('unclear')
  })

  it('never paints a verdict it has never heard of as an all-clear', () => {
    // The five live on the server, so this build can be older than the set.
    // An unrecognised verdict reads as unclear — which is exactly what it is.
    expect(verdictTone('something_new')).toBe('unclear')
    expect(verdictTone('')).toBe('unclear')
  })

  it('renders a verdict word as a label', () => {
    expect(verdictLabel('benign_known')).toBe('BENIGN KNOWN')
  })
})

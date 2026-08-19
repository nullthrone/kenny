import { describe, expect, it } from 'vitest'
import type { TicketEvent } from './types'
import { actorLabel, formatEvent, triageFinding, verdictLabel, verdictTone } from './eventFormat'

function event(overrides: Partial<TicketEvent>): TicketEvent {
  return {
    id: 1,
    ticket_id: 't1',
    at: '2026-08-19T10:00:00Z',
    kind: 'note',
    actor: 'triage',
    tool: null,
    tool_class: null,
    ok: null,
    from_state: null,
    to_state: null,
    summary: '',
    fields: null,
    ...overrides,
  }
}

const VERDICT_FIELDS = {
  verdict: 'phantom',
  finding: 'the device this names is not on this PC',
  evidence: 'diag_services lists only Harddisk0',
  resolvable: true,
}

describe('triageFinding', () => {
  it('reads a verdict off a triage note', () => {
    const found = triageFinding(event({ fields: VERDICT_FIELDS }))
    expect(found).not.toBeNull()
    expect(found?.verdict).toBe('phantom')
    expect(found?.finding).toBe('the device this names is not on this PC')
    expect(found?.evidence).toBe('diag_services lists only Harddisk0')
  })

  it('is null for the "looking into this" note an investigation opens with', () => {
    // An investigation writes two notes and only the second is a finding, so
    // the `verdict` field — not the actor — has to be what decides.
    expect(triageFinding(event({ summary: 'looking into this before anyone is asked to' }))).toBeNull()
  })

  it('is null for a note from anyone else, verdict-shaped or not', () => {
    expect(triageFinding(event({ actor: 'operator:3', fields: VERDICT_FIELDS }))).toBeNull()
  })

  it('is null for a non-note event from triage', () => {
    expect(triageFinding(event({ kind: 'tool_call', fields: VERDICT_FIELDS }))).toBeNull()
  })

  it('carries the reason the server declined to act on a verdict', () => {
    // The most informative row on the page while auto-resolve is still off:
    // it says what would have happened with it on.
    const found = triageFinding(
      event({
        fields: {
          ...VERDICT_FIELDS,
          resolvable: false,
          not_resolved_because: 'no read-only check actually ran',
        },
      }),
    )
    expect(found?.notResolvedBecause).toBe('no read-only check actually ran')
  })

  it('leaves notResolvedBecause null when the verdict was acted on', () => {
    expect(triageFinding(event({ fields: VERDICT_FIELDS }))?.notResolvedBecause).toBeNull()
  })

  it('reads a well-formed suppression suggestion', () => {
    const found = triageFinding(
      event({
        fields: {
          ...VERDICT_FIELDS,
          suppression_suggestion: { source: 'Microsoft-Windows-CAPI2', event_id: 4176 },
        },
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
      const found = triageFinding(event({ fields: { ...VERDICT_FIELDS, suppression_suggestion: bad } }))
      expect(found?.suggestion).toBeNull()
    }
  })

  it('survives fields that are absent or the wrong type', () => {
    const found = triageFinding(event({ fields: { verdict: 'inconclusive', finding: 42 } }))
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

describe('actorLabel', () => {
  it('tells an unprompted investigation apart from kenny answering you', () => {
    expect(actorLabel('assistant', undefined)).toBe('KENNY')
    expect(actorLabel('triage', undefined)).toBe('KENNY · UNPROMPTED')
  })
})

describe('formatEvent', () => {
  it('leaves every other note exactly as it was', () => {
    const f = formatEvent(event({ actor: 'operator:3', summary: 'called the neighbour' }), undefined)
    expect(f.text).toBe('called the neighbour')
    expect(f.mono).toBeNull()
  })
})

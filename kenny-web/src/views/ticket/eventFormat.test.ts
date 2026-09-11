import { describe, expect, it } from 'vitest'
import type { TicketEvent } from './types'
import { actorLabel, formatEvent } from './eventFormat'

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

describe('actorLabel', () => {
  it('tells an unprompted investigation apart from kenny answering you', () => {
    expect(actorLabel('assistant', undefined)).toBe('KENNY')
    expect(actorLabel('triage', undefined)).toBe('KENNY · UNPROMPTED')
  })
})

describe('formatEvent — the audit trail\u2019s own rendering', () => {
  it('keeps a tool call\u2019s arguments verbatim, ok flag and all', () => {
    const f = formatEvent(
      event({ kind: 'tool_call', actor: 'assistant', tool: 'diag_eventlog', ok: true, summary: 'diag_eventlog succeeded', fields: { args: { log: 'System' } } }),
      undefined,
    )
    expect(f.text).toBe('diag_eventlog succeeded')
    expect(f.mono).toBe('diag_eventlog {"log":"System"} · ok')
  })

  it('leaves a note exactly as it was', () => {
    const f = formatEvent(event({ actor: 'operator:3', summary: 'called the neighbour' }), undefined)
    expect(f.text).toBe('called the neighbour')
    expect(f.mono).toBeNull()
  })
})

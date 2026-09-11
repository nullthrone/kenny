import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import AuditTrail from './AuditTrail'
import type { TicketEvent } from './types'
import { KENNY_REPLY } from '../../test/markdownSamples'

function event(over: Partial<TicketEvent>): TicketEvent {
  return {
    id: 1,
    ticket_id: 't-42',
    at: '2026-08-20T06:47:00Z',
    kind: 'message',
    actor: 'assistant',
    tool: null,
    tool_class: null,
    ok: null,
    from_state: null,
    to_state: null,
    summary: 'message',
    fields: null,
    ...over,
  }
}

describe('AuditTrail', () => {
  it('keeps a tool call with the arguments it ran with', () => {
    const { container } = render(
      <AuditTrail
        events={[
          event({
            kind: 'tool_call',
            summary: 'diag_eventlog succeeded',
            tool: 'diag_eventlog',
            ok: true,
            fields: { args: { source: '**DCOM**' } },
          }),
        ]}
      />,
    )

    expect(container.textContent).toContain('diag_eventlog succeeded')
    expect(container.textContent).toContain('"source":"**DCOM**"')
    // Verbatim means verbatim: agent-supplied text is never markup here.
    expect(container.querySelector('strong')).toBeNull()
  })

  it('parses nothing, not even kenny’s own markdown', () => {
    // This is the audit: it shows what is stored. The three surfaces that
    // must render kenny's prose are named in src/test/markdownSamples.ts and
    // this is deliberately not one of them.
    const { container } = render(
      <AuditTrail events={[event({ fields: { text: KENNY_REPLY } })]} />,
    )

    expect(container.querySelector('li')).toBeNull()
    expect(container.textContent).toContain('**Was ich tun kann:**')
  })

  it('names each row, so a timeline entry can be traced back to it', () => {
    const { container } = render(<AuditTrail events={[event({ id: 417 })]} />)
    expect(container.textContent).toContain('#417')
  })

  it('drops nothing — every row of the trail is a row here', () => {
    const { container } = render(
      <AuditTrail
        events={[
          event({ id: 1, kind: 'state', actor: 'system', summary: 'work started' }),
          event({ id: 2, kind: 'note', actor: 'system', summary: 'stall reminder sent (blocked on user)' }),
          event({ id: 3, kind: 'note', actor: 'triage', summary: 'looking into this before anyone is asked to' }),
        ]}
      />,
    )
    expect(container.textContent).toContain('work started')
    expect(container.textContent).toContain('stall reminder sent (blocked on user)')
    expect(container.textContent).toContain('looking into this before anyone is asked to')
  })
})

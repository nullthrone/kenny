import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import Timeline from './Timeline'
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

describe('Timeline', () => {
  it("renders kenny's reply as markdown", () => {
    const { container } = render(
      <Timeline events={[event({ actor: 'assistant', fields: { text: KENNY_REPLY } })]} />,
    )

    expect(container.querySelectorAll('ul li')).toHaveLength(2)
    expect(container.querySelectorAll('ol li')).toHaveLength(2)
    expect(container.querySelector('strong')).not.toBeNull()
    expect(container.textContent).not.toContain('**')
  })

  it('renders a triage note the same way — it is the same assistant', () => {
    const { container } = render(
      <Timeline events={[event({ actor: 'triage', fields: { text: KENNY_REPLY } })]} />,
    )
    expect(container.querySelectorAll('ul li')).toHaveLength(2)
  })

  it("leaves a person's own message unparsed, but keeps their line breaks", () => {
    const { container } = render(
      <Timeline events={[event({ actor: 'operator:1', fields: { text: KENNY_REPLY } })]} />,
    )

    expect(container.querySelector('li')).toBeNull()
    expect(container.querySelector('strong')).toBeNull()
    expect(container.textContent).toContain('**Was ich tun kann:**')
  })

  it('never parses a status line this UI composed', () => {
    const { container } = render(
      <Timeline
        events={[
          event({ kind: 'state', actor: 'system', summary: '- moved to **waiting**', fields: null }),
        ]}
      />,
    )

    expect(container.querySelector('li')).toBeNull()
    expect(container.querySelector('strong')).toBeNull()
    expect(container.textContent).toContain('- moved to **waiting**')
  })

  it('leaves verbatim tool args in the mono block alone', () => {
    const { container } = render(
      <Timeline
        events={[
          event({
            kind: 'tool_call',
            actor: 'assistant',
            summary: 'read the event log',
            tool: 'diag_eventlog',
            ok: true,
            fields: { args: { source: '**DCOM**' } },
          }),
        ]}
      />,
    )

    expect(container.querySelector('strong')).toBeNull()
    expect(container.textContent).toContain('"source":"**DCOM**"'.replace(':', ':'))
  })
})

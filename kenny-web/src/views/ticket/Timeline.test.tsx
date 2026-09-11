import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import Timeline from './Timeline'
import type { TimelineEntry } from './types'
import { KENNY_REPLY } from '../../test/markdownSamples'

function entry(over: Partial<TimelineEntry>): TimelineEntry {
  return {
    at: '2026-08-20T06:47:00Z',
    actor: 'assistant',
    kind: 'message',
    text: 'message',
    body: 'markdown',
    source_event_ids: [1],
    fields: {},
    ...over,
  }
}

/**
 * A verdict's "MUTE ON THIS PC" button is a real mutation, so rendering one
 * needs a client. The rest of the timeline does not — but one wrapper for the
 * whole file keeps the tests about what is rendered rather than about which
 * of them happens to need a provider.
 */
function show(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

describe('Timeline', () => {
  it("renders kenny's reply as markdown", () => {
    const { container } = show(
      <Timeline entries={[entry({ actor: 'assistant', text: KENNY_REPLY })]} />,
    )

    expect(container.querySelectorAll('ul li')).toHaveLength(2)
    expect(container.querySelectorAll('ol li')).toHaveLength(2)
    expect(container.querySelector('strong')).not.toBeNull()
    expect(container.textContent).not.toContain('**')
  })

  it('renders an unprompted investigation the same way — it is the same assistant', () => {
    const { container } = show(
      <Timeline entries={[entry({ actor: 'triage', text: KENNY_REPLY })]} />,
    )
    expect(container.querySelectorAll('ul li')).toHaveLength(2)
    expect(container.textContent).toContain('KENNY · UNPROMPTED')
  })

  it("leaves a person's own message unparsed, but keeps their line breaks", () => {
    const { container } = show(
      <Timeline
        entries={[entry({ actor: 'operator:1', body: 'verbatim', text: KENNY_REPLY })]}
      />,
    )

    expect(container.querySelector('li')).toBeNull()
    expect(container.querySelector('strong')).toBeNull()
    expect(container.textContent).toContain('**Was ich tun kann:**')
  })

  it('never parses a sentence the server composed', () => {
    // The server's own prose is `status`: a tool name beginning with `-` is
    // not a bullet, and event-log text reaching this row is untrusted (ADR-0023).
    const { container } = show(
      <Timeline
        entries={[
          entry({
            kind: 'activity',
            body: 'status',
            text: '- I looked at **the event log**.',
          }),
        ]}
      />,
    )

    expect(container.querySelector('li')).toBeNull()
    expect(container.querySelector('strong')).toBeNull()
    expect(container.textContent).toContain('- I looked at **the event log**.')
  })

  it('renders a verdict as a finding, with its evidence beside it', () => {
    const { container } = show(
      <Timeline
        entries={[
          entry({
            kind: 'finding',
            actor: 'triage',
            body: 'status',
            text: 'triage verdict: phantom - no such device',
            fields: {
              verdict: 'phantom',
              finding: 'The device this names is not on this PC.',
              evidence: 'diag_services lists only Harddisk0.',
            },
          }),
        ]}
      />,
    )

    expect(container.textContent).toContain('PHANTOM')
    expect(container.textContent).toContain('The device this names is not on this PC.')
    expect(container.textContent).toContain('checked: diag_services lists only Harddisk0.')
    // The summary line is the verdict's own row text; the framed card replaces
    // it rather than sitting under a duplicate of it.
    expect(container.textContent).not.toContain('triage verdict: phantom')
  })

  it('shows no raw tool arguments at all — that is the audit tab', () => {
    // The complaint this view exists to answer: the ticket used to carry the
    // machine trail, arguments and all, in the middle of the story.
    const { container } = show(
      <Timeline
        entries={[
          entry({ kind: 'activity', body: 'status', text: 'I looked at the event log (System).' }),
        ]}
      />,
    )
    expect(container.textContent).not.toContain('{')
    expect(container.querySelector('[class*="mono"]')).toBeNull()
  })
})

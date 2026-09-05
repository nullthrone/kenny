import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import type { InboxItem } from '../../api/types'
import InboxRow from './InboxRow'

/**
 * SECURITY-CRITICAL: a gate's `args` are the operator's only evidence of
 * what approving will actually execute (types.ts's `InboxGate` doc
 * comment). This asserts that guarantee survives the whole path from a raw
 * `InboxItem` through `InboxRow` → `ApprovalGate` → `GateCard`, not just
 * `GateCard` in isolation (already covered by
 * `components/GateCard/GateCard.test.tsx`) — including characters that
 * would be mangled by truncation, HTML-escaping, or re-serialisation:
 * backslashes (a Windows path), embedded quotes, and angle brackets.
 */
function renderRow(item: InboxItem) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <InboxRow item={item} onDecided={vi.fn()} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('InboxRow gate rendering', () => {
  it('renders a Windows path and a JSON-ish string, with quotes and backslashes, byte-for-byte', () => {
    const dangerousArgs = {
      path: 'C:\\Users\\oma\\Videos\\Family "Backup" <2026>\\',
      note: '{"already":"json-like","script":"<img src=x onerror=alert(1)>"}',
    }

    const item: InboxItem = {
      id: 'appr-1',
      kind: 'approval',
      waits_on: 'approval',
      severity: null,
      title: 'Ticket #41 — winget_update on mia-desktop',
      meta: 'Requested by mia via Discord · held 38 min',
      host: 'mia-desktop',
      age_seconds: 2280,
      gate: {
        approval_id: 'appr-1',
        ticket_id: '41',
        tool: 'fs_move',
        args: dangerousArgs,
        agent_id: 'mia-desktop',
        tool_class: 'standard_change',
        held_since: '2026-08-17T10:00:00Z',
      },
      target: '#/inbox/ticket/ae73db26ad3e4c078c93050f63395873',
    }

    renderRow(item)

    // Exactly what `GateCard`'s own formatter produces — un-truncated,
    // un-reformatted, keys in server order — must be present as literal text.
    const expected = JSON.stringify(dangerousArgs)
    expect(screen.getByText(expected)).toBeInTheDocument()

    // ...and never parsed as markup: the embedded `<img onerror=...>` must
    // not become a real element, and no literal backslash was stripped.
    expect(document.querySelector('img')).toBeNull()
    expect(expected).toContain('\\\\Users\\\\oma')
    expect(expected).toContain('\\"Backup\\"')
  })
})

/**
 * Every row's title is the way into the thing the row is about, so it must
 * either go somewhere or not look clickable. A `<Link to="">` does neither: it
 * renders as a link and then navigates back to the inbox the reader is already
 * on, which reads as a click that did nothing.
 */
describe('InboxRow title link', () => {
  const base: InboxItem = {
    id: 'section:oma-pc:defender',
    kind: 'section',
    waits_on: 'attention',
    severity: 'crit',
    title: 'real-time protection off',
    meta: 'defender',
    host: 'oma-pc',
    age_seconds: 60,
    gate: null,
    target: '#/fleet/oma-pc?section=defender',
  }

  it('links a flagged section to that section, not to the bare host page', () => {
    renderRow(base)

    expect(screen.getByRole('link', { name: base.title })).toHaveAttribute(
      'href',
      expect.stringContaining('/fleet/oma-pc?section=defender'),
    )
  })

  it('renders a row with no route as plain text rather than a dead link', () => {
    renderRow({ ...base, target: '' })

    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.getByText(base.title)).toBeInTheDocument()
  })
})

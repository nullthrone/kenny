import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { InboxItem } from '../../api/types'
import InboxRow from './InboxRow'

function renderRow(item: InboxItem) {
  render(
    <MemoryRouter>
      <InboxRow item={item} />
    </MemoryRouter>,
  )
}

const base: InboxItem = {
  id: 'ae73db26ad3e4c078c93050f63395873',
  waits_on: '',
  priority: 'normal',
  title: 'oma-pc: real-time protection off',
  meta: '#41 · alert',
  host: 'oma-pc',
  age_seconds: 2280,
  target: '#/inbox/ticket/ae73db26ad3e4c078c93050f63395873',
}

/**
 * Every row's title is the way into the ticket it is about, so it must either
 * go somewhere or not look clickable. A `<Link to="">` does neither: it
 * renders as a link and then navigates back to the inbox the reader is already
 * on, which reads as a click that did nothing.
 */
describe('InboxRow title link', () => {
  it('links a row to its ticket', () => {
    renderRow(base)

    expect(screen.getByRole('link', { name: base.title })).toHaveAttribute(
      'href',
      expect.stringContaining('/inbox/ticket/ae73db26ad3e4c078c93050f63395873'),
    )
  })

  it('renders a row with no route as plain text rather than a dead link', () => {
    renderRow({ ...base, target: '' })

    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.getByText(base.title)).toBeInTheDocument()
  })
})

/**
 * The queue says what a ticket is waiting for; it does not offer the decision.
 * Approving belongs on the ticket, where the frozen call it would run is
 * shown — deciding from a list, next to a title, is deciding without the
 * evidence (`views/ticket/ApprovalGate.test.tsx` pins that surface).
 */
describe('InboxRow offers no decision', () => {
  it('shows that a ticket waits for an approval without offering one', () => {
    renderRow({ ...base, waits_on: 'approval', meta: '#41 · alert · waiting for approval' })

    expect(screen.getByText('#41 · alert · waiting for approval')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /approve|deny/i })).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
  })
})

/** The badge is the row's priority, and an unknown value must render dully. */
describe('InboxRow priority badge', () => {
  it.each([
    ['urgent', 'URGENT'],
    ['high', 'HIGH'],
    ['normal', 'NORMAL'],
    ['low', 'LOW'],
  ])('renders %s as %s', (priority, label) => {
    renderRow({ ...base, priority: priority as InboxItem['priority'] })

    expect(screen.getByText(label)).toBeInTheDocument()
  })

  it('renders a priority the console has no colour for rather than crashing', () => {
    renderRow({ ...base, priority: 'catastrophic' as InboxItem['priority'] })

    expect(screen.getByText('CATASTROPHIC')).toBeInTheDocument()
  })
})

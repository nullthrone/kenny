import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Ticket } from './types'

const { apiPostMock } = vi.hoisted(() => ({ apiPostMock: vi.fn() }))
vi.mock('../../api/client', () => ({
  api: { get: vi.fn(), post: apiPostMock, put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))

const { default: TicketActions } = await import('./TicketActions')

function ticket(over: Partial<Ticket> = {}): Ticket {
  return {
    id: 'tkt_1',
    state: 'in_progress',
    blocked_on: '',
    allowed_transitions: [],
    can_unblock: false,
    agent_id: 'oma-pc',
    ...over,
  } as Ticket
}

function renderActions(over: Partial<Ticket> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <TicketActions ticket={ticket(over)} onMutated={vi.fn()} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  apiPostMock.mockReset()
  apiPostMock.mockResolvedValue({})
})

/**
 * The server decides what may render: `_affordances` computes
 * `allowed_transitions`/`can_unblock` per principal, so a button appearing here
 * means the API would accept it from the account looking at it. Nothing in the
 * component infers legality from `state` — `state` only chooses *which* of the
 * licensed moves is the forward one, and what to call it.
 */
describe('TicketActions — one forward move', () => {
  it('promotes the move that carries the ticket onward, and names it for the state it leaves', () => {
    renderActions({ state: 'in_progress', allowed_transitions: ['resolved', 'cancelled'] })
    expect(screen.getByText('MARK RESOLVED')).toBeInTheDocument()
  })

  it('calls a resolved ticket’s return to work REOPEN, not START WORK', () => {
    // Same destination as `new -> in_progress`, opposite meaning. Keying the
    // label on the destination alone is what had this button reading "START
    // WORK" on a ticket that had already been resolved once.
    renderActions({ state: 'resolved', allowed_transitions: ['closed', 'in_progress'] })
    expect(screen.getByText('CLOSE NOW')).toBeInTheDocument()
    expect(screen.getByText('REOPEN')).toBeInTheDocument()
    expect(screen.queryByText('START WORK')).not.toBeInTheDocument()
  })

  it('posts the transition the button stands for', async () => {
    renderActions({ state: 'new', allowed_transitions: ['in_progress'] })

    fireEvent.click(screen.getByText('START WORK'))

    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/tickets/tkt_1/transition', {
        to: 'in_progress',
        reason: '',
      }),
    )
  })

  it('closes through its own route, because closing settles the record', async () => {
    renderActions({ state: 'resolved', allowed_transitions: ['closed'] })

    fireEvent.click(screen.getByText('CLOSE NOW'))

    await waitFor(() => expect(apiPostMock).toHaveBeenCalledWith('/api/tickets/tkt_1/close', {}))
  })

  it('renders nothing at all when the server licenses nothing', () => {
    const { container } = renderActions({ state: 'closed', allowed_transitions: [] })
    expect(container).toBeEmptyDOMElement()
  })
})

describe('TicketActions — clearing a block', () => {
  it('says what the click asserts, per reason', () => {
    renderActions({ blocked_on: 'user', can_unblock: true })
    expect(screen.getByText('GOT AN ANSWER')).toBeInTheDocument()
  })

  it('offers to pick up an escalated ticket', () => {
    renderActions({ blocked_on: 'operator', can_unblock: true })
    expect(screen.getByText('PICK THIS UP')).toBeInTheDocument()
  })

  it('offers nothing when the server says this principal may not', () => {
    // Including a ticket on a live gate: `_UNBLOCK_CLEARERS["approval"]` is
    // `{"system"}`, so `can_unblock` is false and the way out is the decision
    // itself, made in the drawer beside the frozen call.
    renderActions({ blocked_on: 'approval', can_unblock: false, allowed_transitions: ['resolved'] })
    expect(screen.queryByText('GOT AN ANSWER')).not.toBeInTheDocument()
    expect(screen.queryByText('PICK THIS UP')).not.toBeInTheDocument()
  })
})

describe('TicketActions — what this surface no longer offers', () => {
  it('never lets a person declare a ticket to be waiting on something', () => {
    // Blocks are written where the wait begins, carrying the ref that
    // identifies it. Set by hand they produced a wait with no referent — an
    // `approval` block with no approval behind it, which no clock could clear.
    renderActions({ allowed_transitions: ['resolved', 'cancelled'] })
    expect(screen.queryByText(/WAIT ON/)).not.toBeInTheDocument()
  })

  it('offers no claim and no host change', () => {
    renderActions({ allowed_transitions: ['resolved', 'cancelled'] })
    expect(screen.queryByText(/CLAIM/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/REASSIGN/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Reassign to host')).not.toBeInTheDocument()
  })

  it('keeps the whole surface to the forward move and one quiet exit', () => {
    renderActions({ state: 'in_progress', allowed_transitions: ['resolved', 'cancelled'] })
    expect(screen.getAllByRole('button')).toHaveLength(2)
  })
})

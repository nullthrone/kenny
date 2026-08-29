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
    allowed_transitions: [],
    allowed_blocks: [],
    can_unblock: false,
    assignee_user_id: null,
    agent_id: 'oma-pc',
    ...over,
  } as Ticket
}

function renderActions(over: Partial<Ticket> = {}, isOperator = true) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <TicketActions
        ticket={ticket(over)}
        isOperator={isOperator}
        meUserId={7}
        fleetAgents={[]}
        onMutated={vi.fn()}
      />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  apiPostMock.mockReset()
  apiPostMock.mockResolvedValue({})
})

/**
 * Blocking is what puts a ticket into the Inbox's WAITING group. Through 2.2.0
 * the console could unblock but never block: `allowed_blocks` arrived on every
 * ticket payload, was declared on the `Ticket` type, and was never rendered — so
 * WAITING had no entrance from the dashboard at all.
 *
 * As with transitions, the server decides. `_affordances` computes
 * `allowed_blocks` per principal, so a button appearing here means the API would
 * accept it from the account looking at it; nothing below infers it from `state`.
 */
describe('TicketActions — blocking', () => {
  it('renders one button per reason the server allows', async () => {
    renderActions({ allowed_blocks: ['user', 'operator'] })

    expect(screen.getByText('WAIT ON REQUESTER')).toBeInTheDocument()
    expect(screen.getByText('WAIT ON OPERATOR')).toBeInTheDocument()
  })

  it('posts the reason the button stands for', async () => {
    renderActions({ allowed_blocks: ['user'] })

    fireEvent.click(screen.getByText('WAIT ON REQUESTER'))

    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/tickets/tkt_1/block', { blocked_on: 'user' }),
    )
  })

  it('renders no block button when the server allows none', () => {
    renderActions({ allowed_blocks: [], allowed_transitions: ['resolved'] })

    expect(screen.queryByText(/^WAIT ON/)).not.toBeInTheDocument()
    expect(screen.getByText('MARK RESOLVED')).toBeInTheDocument()
  })

  it('names an unknown reason rather than hiding it — the server owns the vocabulary', () => {
    renderActions({ allowed_blocks: ['third_party'] })

    expect(screen.getByText('WAIT ON THIRD PARTY')).toBeInTheDocument()
  })

  it('still renders for a scoped user whose only affordance is a block', () => {
    renderActions({ allowed_blocks: ['user'] }, false)

    expect(screen.getByText('WAIT ON REQUESTER')).toBeInTheDocument()
    // Operator-only controls stay hidden.
    expect(screen.queryByText('CLAIM')).not.toBeInTheDocument()
  })
})

describe('TicketActions — unblocking', () => {
  it('offers UNBLOCK only when the server says this principal may', () => {
    renderActions({ can_unblock: true })
    expect(screen.getByText('UNBLOCK')).toBeInTheDocument()
  })

  it('hides UNBLOCK otherwise', () => {
    renderActions({ can_unblock: false, allowed_transitions: ['resolved'] })
    expect(screen.queryByText('UNBLOCK')).not.toBeInTheDocument()
  })
})

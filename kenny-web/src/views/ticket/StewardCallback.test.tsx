import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { TicketCallbackView } from './StewardCallback'

const { apiGetMock, apiPostMock, apiDeleteMock } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  apiPostMock: vi.fn(),
  apiDeleteMock: vi.fn(),
}))
vi.mock('../../api/client', async (importOriginal) => {
  const real = await importOriginal<typeof import('../../api/client')>()
  return {
    ...real,
    api: { get: apiGetMock, post: apiPostMock, put: vi.fn(), patch: vi.fn(), delete: apiDeleteMock },
  }
})

const { default: StewardCallback } = await import('./StewardCallback')
const { ApiError } = await import('../../api/client')

function view(over: Partial<TicketCallbackView> = {}): TicketCallbackView {
  return {
    ticket_id: 'tkt_1',
    host_id: 'marianne-pc',
    sections: ['defender'],
    callback: null,
    can_arm: true,
    reason: null,
    ttl_days: 14,
    max_attempts: 3,
    stewards: [
      {
        steward_id: 'fixer',
        title: 'Fixer',
        summary: '',
        mode: 'act',
        effective_hash: 'h-shown',
        allowed: true,
        reason: null,
        code: null,
      },
    ],
    ...over,
  }
}

function renderIt(onMutated = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <StewardCallback ticketId="tkt_1" onMutated={onMutated} />
    </QueryClientProvider>,
  )
  return onMutated
}

beforeEach(() => {
  apiGetMock.mockReset()
  apiPostMock.mockReset()
  apiDeleteMock.mockReset()
})

describe('StewardCallback', () => {
  it('hands the ticket over with the hash the server showed', async () => {
    apiGetMock.mockResolvedValue(view())
    apiPostMock.mockResolvedValue({})
    const onMutated = renderIt()
    fireEvent.click(await screen.findByRole('checkbox'))
    expect(screen.getByText(/at most 3 times within 14 days/)).toBeInTheDocument()
    fireEvent.click(screen.getByText('HAND OVER'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/tickets/tkt_1/callback', {
        steward_id: 'fixer',
        effective_hash: 'h-shown',
      }),
    )
    await waitFor(() => expect(onMutated).toHaveBeenCalled())
  })

  it('renders nothing for a ticket no server check can judge', async () => {
    apiGetMock.mockResolvedValue(view({ sections: [], stewards: [], can_arm: false, reason: 'no' }))
    renderIt()
    await waitFor(() => expect(apiGetMock).toHaveBeenCalled())
    expect(screen.queryByText('Steward callback')).not.toBeInTheDocument()
  })

  it('shows a live callback and withdraws it', async () => {
    apiGetMock.mockResolvedValue(
      view({
        can_arm: false,
        reason: 'a callback is already armed on this ticket',
        callback: {
          id: 'cb1',
          ticket_id: 'tkt_1',
          steward_id: 'fixer',
          host_id: 'marianne-pc',
          effective_hash: 'h',
          sections: ['defender'],
          armed_by: 'admin',
          armed_at: '2026-11-01T10:00:00+00:00',
          expires_at: '2026-11-15T10:00:00+00:00',
          max_attempts: 3,
          attempts: 1,
          state: 'armed',
          last_run_id: 'r1',
          run_finished_at: null,
          last_error: 'defender still unhealthy after run r1',
          ended_at: null,
          ended_by: null,
          live: true,
        },
      }),
    )
    apiDeleteMock.mockResolvedValue({})
    renderIt()
    expect(await screen.findByText(/waiting for the machine/)).toBeInTheDocument()
    expect(screen.getByText(/Attempt 1 of/)).toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('WITHDRAW'))
    await waitFor(() => expect(apiDeleteMock).toHaveBeenCalledWith('/api/tickets/tkt_1/callback'))
  })

  it('says the steward changed on a stale hash', async () => {
    apiGetMock.mockResolvedValue(view())
    apiPostMock.mockRejectedValue(new ApiError('stale', 409, 'hash_stale'))
    renderIt()
    fireEvent.click(await screen.findByRole('checkbox'))
    fireEvent.click(screen.getByText('HAND OVER'))
    expect(await screen.findByText(/The steward changed/)).toBeInTheDocument()
  })
})

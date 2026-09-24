import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { apiGetMock } = vi.hoisted(() => ({ apiGetMock: vi.fn() }))
vi.mock('../api/client', () => ({
  api: { get: apiGetMock, post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  ApiError: class ApiError extends Error {},
}))

const { default: InboxTicket } = await import('./InboxTicket')

const TICKET = {
  id: 't-1',
  number: 7,
  title: 'printer',
  state: 'in_progress',
  origin: 'dashboard',
  priority: 'normal',
  category: null,
  requester_user_id: null,
  agent_id: 'pc',
  role_snapshot: null,
  profile_snapshot: null,
  summary: '',
  resolution: null,
  created_at: '2026-09-24T10:00:00Z',
  updated_at: '2026-09-24T10:00:00Z',
  closed_at: null,
  blocked_on: 'approval',
  blocked_since: '2026-09-24T10:01:00Z',
  blocked_ref: 'ap-1',
  assignee_user_id: null,
  resolved_by: '',
  allowed_transitions: [],
  allowed_blocks: [],
  can_unblock: false,
  assistant_available: true,
}

function mockApi(ticketAssistant: boolean) {
  const routes: Record<string, unknown> = {
    '/api/me': { user_id: '1', username: 'thomas', role: 'superuser', hosts: [], theme: null, is_shared_token: false },
    '/api/users/directory': { users: [] },
    '/api/tickets/vocabulary': { states: [], blocked_reasons: [], priorities: [], categories: [] },
    '/api/tickets/t-1': { ...TICKET, assistant_available: ticketAssistant },
    '/api/tickets/t-1/alerts': { alerts: [], findings: [] },
    '/api/tickets/t-1/timeline': { entries: [] },
    '/api/approvals?ticket_id=t-1': {
      approvals: [{ id: 'ap-1', status: 'pending', tool: 'winget_update', args: {}, agent_id: 'pc' }],
    },
    '/api/ai/status': {
      enabled: true,
      configured: true,
      source: 'db',
      features: { ask: true, ticket_assistant: ticketAssistant },
    },
  }
  apiGetMock.mockImplementation((path: string) => Promise.resolve(routes[path] ?? {}))
}

function renderTicket() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/inbox/ticket/t-1']}>
        <Routes>
          <Route path="/inbox/ticket/:id" element={<InboxTicket />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => apiGetMock.mockReset())

/**
 * A pending decision is answered in the Ask kenny drawer, which the shell offers
 * on a ticket only while the ticket assistant is on. The pointer to it follows
 * the same switch, so it never points at a drawer that is not there.
 */
describe('InboxTicket — the pending decision follows the ticket assistant', () => {
  it('points to the drawer while the assistant is on', async () => {
    mockApi(true)
    renderTicket()
    expect(await screen.findByRole('button', { name: 'ANSWER IN ASK KENNY' })).toBeInTheDocument()
  })

  it('shows nothing of it while the assistant is off', async () => {
    mockApi(false)
    renderTicket()
    await screen.findByText(/Ticket #7/)
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/ai/status'))
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/approvals?ticket_id=t-1'))
    expect(screen.queryByRole('button', { name: 'ANSWER IN ASK KENNY' })).not.toBeInTheDocument()
    expect(screen.queryByText(/waiting on a decision/)).not.toBeInTheDocument()
  })
})

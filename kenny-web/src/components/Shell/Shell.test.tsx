import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { apiGetMock, apiPutMock } = vi.hoisted(() => ({ apiGetMock: vi.fn(), apiPutMock: vi.fn() }))
vi.mock('../../api/client', () => ({
  api: { get: apiGetMock, post: vi.fn(), put: apiPutMock, patch: vi.fn(), delete: vi.fn() },
}))

const { default: Shell } = await import('./Shell')
const { ThemeProvider } = await import('../../theme/ThemeProvider')

const ME = { user_id: '1', username: 'thomas', role: 'superuser', hosts: [], theme: null, is_shared_token: false }
const NO_TICKETS = { needs_you: 0, waiting: 0, working: 0, new: 0, done: 0 }
const FLEET = {
  agents: [
    { agent_id: 'a', online: true },
    { agent_id: 'b', online: true },
  ],
}

function mockApi(over: Record<string, unknown> = {}) {
  const routes: Record<string, unknown> = {
    '/api/me': ME,
    '/api/fleet': FLEET,
    '/api/about': { server_version: '2.2.0', protocol_version: '0.17', repo: 'nullthrone/kenny' },
    '/api/tickets/summary': NO_TICKETS,
    ...over,
  }
  apiGetMock.mockImplementation((path: string) => {
    const hit = routes[path]
    if (hit === undefined) return Promise.resolve({})
    return hit instanceof Error ? Promise.reject(hit) : Promise.resolve(hit)
  })
}

function renderShell() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <MemoryRouter initialEntries={['/today']}>
          <Shell />
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>,
  )
}

/** The sidebar nav. The mobile tab bar renders the same links and is only hidden by CSS. */
function sidebarNav() {
  return within(screen.getByRole('navigation', { name: 'Sections' }))
}

beforeEach(() => {
  apiGetMock.mockReset()
  apiPutMock.mockReset()
  apiPutMock.mockResolvedValue({ theme: 'light', stored: true })
  localStorage.clear()
  document.documentElement.setAttribute('data-theme', 'light')
})

describe('Shell', () => {
  it('carries the running server version on the fleet line', async () => {
    mockApi()
    renderShell()
    await waitFor(() => expect(screen.getByText(/v2\.2\.0 ·/)).toBeInTheDocument())
    expect(screen.getByText(/2 agents · all reporting/)).toBeInTheDocument()
  })

  it('opens the About dialog from the fleet line', async () => {
    mockApi()
    renderShell()
    const trigger = await screen.findByRole('button', { name: /About kenny/ })
    fireEvent.click(trigger)
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText('ABOUT KENNY')).toBeInTheDocument()
  })

  /**
   * The fleet line must degrade to what it said before About existed, rather
   * than rendering "vundefined" — /api/about is not load-bearing for the shell.
   */
  it('drops the version segment when /api/about fails, and stays clickable', async () => {
    mockApi({ '/api/about': new Error('nope') })
    renderShell()
    const trigger = await screen.findByRole('button', { name: 'About kenny' })
    await waitFor(() => expect(trigger).toHaveTextContent('2 agents · all reporting'))
    expect(trigger.textContent).not.toContain('undefined')
    expect(trigger.textContent).not.toMatch(/^v/)
  })
})

/**
 * The Inbox badge mirrors the queue's NEEDS YOU count. Every part of it existed
 * through 2.2.0 — the prop, the markup, the `TicketSummary` type naming this
 * exact use, the endpoint — with nothing joining them, so the count never
 * appeared. The seam is Shell fetching `/api/tickets/summary` and feeding it to
 * both nav surfaces.
 */
describe('Shell — the Inbox badge', () => {
  it('shows the NEEDS YOU count on the Inbox nav item', async () => {
    mockApi({ '/api/tickets/summary': { ...NO_TICKETS, needs_you: 3 } })
    renderShell()

    const inbox = await sidebarNav().findByRole('link', { name: /INBOX/ })
    await waitFor(() => expect(inbox).toHaveTextContent('3'))
  })

  it('caps a large count rather than widening the sidebar', async () => {
    mockApi({ '/api/tickets/summary': { ...NO_TICKETS, needs_you: 250 } })
    renderShell()

    const inbox = await sidebarNav().findByRole('link', { name: /INBOX/ })
    await waitFor(() => expect(inbox).toHaveTextContent('99+'))
  })

  it('shows no badge when nothing needs you — the ordinary state is not a zero to clear', async () => {
    mockApi()
    renderShell()

    const inbox = await sidebarNav().findByRole('link', { name: /INBOX/ })
    await waitFor(() => expect(screen.getByText(/2 agents/)).toBeInTheDocument())
    expect(inbox.textContent).toBe('INBOX')
  })

  it('renders the rest of the chrome when the count cannot be read', async () => {
    mockApi({ '/api/tickets/summary': new Error('boom') })
    renderShell()

    expect(await sidebarNav().findByRole('link', { name: /INBOX/ })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText(/2 agents · all reporting/)).toBeInTheDocument())
  })
})

/**
 * ADMIN is operator+: every section behind it floors at `operator` server-side,
 * so a `user` following the link only ever reaches a 403. LOG deliberately stays
 * — `/api/log` floors at `user` and narrows the rows to their own hosts.
 */
describe('Shell — role-gated navigation', () => {
  it('hides ADMIN from a scoped user', async () => {
    mockApi({ '/api/me': { ...ME, role: 'user', hosts: ['oma-pc'] } })
    renderShell()

    await sidebarNav().findByRole('link', { name: /INBOX/ })
    expect(sidebarNav().queryByRole('link', { name: /ADMIN/ })).not.toBeInTheDocument()
    expect(sidebarNav().getByRole('link', { name: /LOG/ })).toBeInTheDocument()
  })

  it('shows ADMIN to an operator', async () => {
    mockApi({ '/api/me': { ...ME, role: 'operator' } })
    renderShell()

    expect(await sidebarNav().findByRole('link', { name: /ADMIN/ })).toBeInTheDocument()
  })
})

/**
 * The account's stored theme wins over this browser's copy on load. The inline
 * boot script has already painted from localStorage, so this only corrects a
 * browser the operator has not set a theme on — and it must not write back, or
 * the server's own value would echo round on every load.
 */
describe('Shell — the account theme', () => {
  it('adopts the theme stored on the account', async () => {
    mockApi({ '/api/me': { ...ME, theme: 'dark' } })
    renderShell()

    await waitFor(() => expect(document.documentElement.getAttribute('data-theme')).toBe('dark'))
    expect(apiPutMock).not.toHaveBeenCalled()
  })

  it('keeps the browser copy for an identity with nothing stored', async () => {
    mockApi({ '/api/me': { ...ME, theme: null, is_shared_token: true } })
    renderShell()

    await waitFor(() => expect(screen.getByText(/2 agents/)).toBeInTheDocument())
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
  })
})

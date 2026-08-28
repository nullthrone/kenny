import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { apiGetMock } = vi.hoisted(() => ({ apiGetMock: vi.fn() }))
vi.mock('../../api/client', () => ({
  api: { get: apiGetMock, post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))

const { default: Shell } = await import('./Shell')
const { ThemeProvider } = await import('../../theme/ThemeProvider')

const ME = { user_id: '1', username: 'thomas', role: 'superuser', hosts: [], is_shared_token: false }
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
    '/api/about': { server_version: '2.2.0', protocol_version: '0.17', repo: 't11z/kenny' },
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

beforeEach(() => {
  apiGetMock.mockReset()
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

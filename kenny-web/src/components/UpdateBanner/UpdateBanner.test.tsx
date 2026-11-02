import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { apiGetMock, apiPostMock } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  apiPostMock: vi.fn(),
}))
vi.mock('../../api/client', () => ({
  api: { get: apiGetMock, post: apiPostMock, put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))

const { default: UpdateBanner } = await import('./UpdateBanner')

const LINUS = {
  agent_id: 'linus-pc',
  online: true,
  os: 'windows',
  arch: 'x86_64',
  channel: 'stable',
  current_version: '2.5.1',
  staged_version: '2.7.0',
}
const THOMAS = { ...LINUS, agent_id: 'thomas-pc', current_version: '2.6.0' }
const MARIA = { ...LINUS, agent_id: 'maria-pc', online: false }

function mockApi(agents: unknown[], role = 'operator') {
  const routes: Record<string, unknown> = {
    '/api/me': { user_id: 'u1', username: 'op', role, hosts: [] },
    '/api/agents/outdated': { agents },
  }
  apiGetMock.mockImplementation((path: string) => Promise.resolve(routes[path]))
}

function renderBanner() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <UpdateBanner />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  apiGetMock.mockReset()
  apiPostMock.mockReset()
})
afterEach(() => vi.restoreAllMocks())

describe('UpdateBanner', () => {
  it('stays out of the way when every agent is current', async () => {
    mockApi([])
    renderBanner()
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/agents/outdated'))
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('shows nothing to a scoped user, who cannot act on it and is not even asked', async () => {
    mockApi([LINUS], 'user')
    renderBanner()
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/me'))
    expect(apiGetMock).not.toHaveBeenCalledWith('/api/agents/outdated')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('lists who is behind and updates one agent by name', async () => {
    mockApi([LINUS, THOMAS])
    apiPostMock.mockResolvedValue({ ok: true, sent: 1, results: [{ agent_id: 'linus-pc', status: 'sent' }] })
    renderBanner()

    expect(await screen.findByText(/2 PCs run an older agent than the staged 2\.7\.0/)).toBeInTheDocument()
    expect(screen.getByText('2.5.1 → 2.7.0')).toBeInTheDocument()

    const row = screen.getByText('linus-pc').closest('li') as HTMLElement
    fireEvent.click(row.querySelector('button') as HTMLButtonElement)

    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/agents/update-outdated', { agents: ['linus-pc'] }),
    )
    expect(await screen.findByText(/Sent to 1/)).toBeInTheDocument()
  })

  it('updates all online agents in one request, after a confirmation naming them', async () => {
    mockApi([LINUS, THOMAS, MARIA])
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    apiPostMock.mockResolvedValue({ ok: true, sent: 2, results: [] })
    renderBanner()

    fireEvent.click(await screen.findByRole('button', { name: /UPDATE ALL ONLINE \(2\)/ }))

    expect(confirm.mock.calls[0][0]).toContain('linus-pc, thomas-pc')
    expect(confirm.mock.calls[0][0]).not.toContain('maria-pc')
    await waitFor(() => expect(apiPostMock).toHaveBeenCalledWith('/api/agents/update-outdated', undefined))
  })

  it('sends nothing when the confirmation is declined', async () => {
    mockApi([LINUS, THOMAS])
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderBanner()
    fireEvent.click(await screen.findByRole('button', { name: /UPDATE ALL ONLINE/ }))
    expect(apiPostMock).not.toHaveBeenCalled()
  })

  it('offers no button for an offline agent', async () => {
    mockApi([MARIA])
    renderBanner()
    expect(await screen.findByText(/offline — offered again on reconnect/)).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('reports a failed agent without hiding the ones that went through', async () => {
    mockApi([LINUS, THOMAS])
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    apiPostMock.mockResolvedValue({
      ok: true,
      sent: 1,
      results: [
        { agent_id: 'linus-pc', status: 'sent' },
        { agent_id: 'thomas-pc', status: 'failed', error: 'remote control is off' },
      ],
    })
    renderBanner()
    fireEvent.click(await screen.findByRole('button', { name: /UPDATE ALL ONLINE/ }))
    expect(await screen.findByText(/Sent to 1/)).toBeInTheDocument()
    expect(screen.getByText(/thomas-pc: remote control is off/)).toBeInTheDocument()
  })
})

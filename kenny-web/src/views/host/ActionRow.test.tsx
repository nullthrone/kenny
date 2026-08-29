import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { apiGetMock, apiPostMock } = vi.hoisted(() => ({ apiGetMock: vi.fn(), apiPostMock: vi.fn() }))
vi.mock('../../api/client', () => ({
  api: {
    get: apiGetMock,
    post: apiPostMock,
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}))

const { default: ActionRow } = await import('./ActionRow')

function renderRow(props: Partial<React.ComponentProps<typeof ActionRow>> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ActionRow agentId="oma-pc" os="windows" {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  apiGetMock.mockReset()
  apiPostMock.mockReset()
})

/**
 * This row is the client half of a seam: which buttons it draws must match the
 * `min_role` on the routes behind them. Only REFRESH and REMOTE HELP floor at
 * `user` (`scoped`); everything else is operator+. When only REINSTALL and
 * RE-SHARE were gated, a scoped `user` was offered UPDATE AGENT, the channel
 * selector and a REMOVE button that opens a "this cannot be undone" dialog and
 * then 403s. If either side moves, these lists stop matching.
 */
const USER_ACTIONS = ['REFRESH', 'REMOTE HELP']
const OPERATOR_ACTIONS = ['REINSTALL', 'RE-SHARE', 'UPDATE AGENT', 'REMOVE']

describe('ActionRow — operator gating', () => {
  it('offers a scoped user only the two actions their role can actually run', async () => {
    apiGetMock.mockResolvedValue({ user_id: '1', username: 'mia', role: 'user', hosts: ['oma-pc'], is_shared_token: false })

    renderRow()

    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/me'))
    for (const label of USER_ACTIONS) expect(screen.getByText(label)).toBeInTheDocument()
    for (const label of OPERATOR_ACTIONS) expect(screen.queryByText(label)).not.toBeInTheDocument()
    // PUT /api/agent/{id}/channel is operator-scoped too, so the selector goes with them.
    expect(screen.queryByText('CHANNEL')).not.toBeInTheDocument()
  })

  it('offers an operator the whole row', async () => {
    apiGetMock.mockResolvedValue({ user_id: '1', username: 'thomas', role: 'operator', hosts: [], is_shared_token: false })

    renderRow()

    expect(await screen.findByText('REINSTALL')).toBeInTheDocument()
    for (const label of [...USER_ACTIONS, ...OPERATOR_ACTIONS]) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
    expect(screen.getByText('CHANNEL')).toBeInTheDocument()
  })
})

describe('ActionRow — REINSTALL', () => {
  it('navigates the browser directly to the installer route, not a fetch', async () => {
    apiGetMock.mockResolvedValue({ user_id: '1', username: 'thomas', role: 'superuser', hosts: [], is_shared_token: false })
    const originalLocation = window.location
    // jsdom's real navigation isn't implemented; swap in a plain object so
    // the assignment (`window.location.href = ...`, not `location.assign`)
    // can be observed instead of throwing.
    Object.defineProperty(window, 'location', { value: { href: '' }, writable: true })

    renderRow({ os: 'linux', arch: 'arm64' })
    fireEvent.click(await screen.findByText('REINSTALL'))

    expect(window.location.href).toBe('/api/agents/oma-pc/installer?os=linux&arch=arm64')
    expect(apiPostMock).not.toHaveBeenCalled()

    Object.defineProperty(window, 'location', { value: originalLocation, writable: true })
  })
})

describe('ActionRow — RE-SHARE', () => {
  it('posts {name, os} to /api/agents/share-link and shows the returned single-use URL and expiry', async () => {
    apiGetMock.mockResolvedValue({ user_id: '1', username: 'thomas', role: 'operator', hosts: [], is_shared_token: false })
    apiPostMock.mockResolvedValue({
      url: 'https://kenny.local/o/abc123',
      expires_at: '2026-08-18T00:00:00Z',
      os: 'windows',
      name: 'oma-pc',
    })

    renderRow()
    fireEvent.click(await screen.findByText('RE-SHARE'))

    await waitFor(() => expect(apiPostMock).toHaveBeenCalledWith('/api/agents/share-link', { name: 'oma-pc', os: 'windows' }))
    expect(await screen.findByDisplayValue('https://kenny.local/o/abc123')).toBeInTheDocument()
    expect(screen.getByText(/works once/i)).toBeInTheDocument()
    // No macOS/Windows-only "oneliner" row for a Windows host.
    expect(screen.queryByLabelText('Install one-liner')).not.toBeInTheDocument()
  })

  it('shows the Linux one-liner alongside the URL when the response carries one', async () => {
    apiGetMock.mockResolvedValue({ user_id: '1', username: 'thomas', role: 'operator', hosts: [], is_shared_token: false })
    apiPostMock.mockResolvedValue({
      url: 'https://kenny.local/o/def456',
      expires_at: '2026-08-18T00:00:00Z',
      os: 'linux',
      name: 'garage-pi',
      oneliner: 'curl -fsSL https://kenny.local/o/def456 | sudo sh',
    })

    renderRow({ agentId: 'garage-pi', os: 'linux' })
    fireEvent.click(await screen.findByText('RE-SHARE'))

    await waitFor(() => expect(apiPostMock).toHaveBeenCalledWith('/api/agents/share-link', { name: 'garage-pi', os: 'linux' }))
    expect(await screen.findByDisplayValue('curl -fsSL https://kenny.local/o/def456 | sudo sh')).toBeInTheDocument()
  })

  it('reports a mint failure instead of showing a stale or blank link', async () => {
    apiGetMock.mockResolvedValue({ user_id: '1', username: 'thomas', role: 'operator', hosts: [], is_shared_token: false })
    apiPostMock.mockRejectedValue(new Error('forbidden'))

    renderRow()
    fireEvent.click(await screen.findByText('RE-SHARE'))

    expect(await screen.findByText('Could not create the link: forbidden.')).toBeInTheDocument()
  })
})

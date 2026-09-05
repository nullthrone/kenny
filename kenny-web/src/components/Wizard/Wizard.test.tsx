import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentBinaryStatus } from '../../api/types'

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

const { default: Wizard } = await import('./Wizard')

/** The shape `distribution.agent_binary_status` returns for a healthy dual-target server. */
const BINARY_OK: AgentBinaryStatus = {
  version: '2.2.0',
  available: true,
  by_os: { windows: true, linux: true },
  targets: [
    { os: 'windows', arch: 'x86_64', available: true },
    { os: 'linux', arch: 'x86_64', available: true },
    { os: 'linux', arch: 'aarch64', available: true },
  ],
  repo: 'nullthrone/kenny',
  last_fetch: { ok: true, message: 'fetched 2.2.0' },
}

beforeEach(() => {
  apiGetMock.mockReset()
  apiPostMock.mockReset()
  apiGetMock.mockResolvedValue(BINARY_OK)
})

function renderWizard(ui: React.ReactElement) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>)
}

function goToStep2() {
  fireEvent.change(screen.getByPlaceholderText('e.g. tante-laptop'), { target: { value: 'tante-laptop' } })
  fireEvent.click(screen.getByText('NEXT'))
}

describe('Wizard', () => {
  it('keeps NEXT disabled until the machine name is a valid slug', () => {
    renderWizard(<Wizard open onClose={vi.fn()} />)
    expect(screen.getByText('NEXT')).toBeDisabled()

    fireEvent.change(screen.getByPlaceholderText('e.g. tante-laptop'), { target: { value: 'Tante Laptop' } })
    expect(screen.getByText('NEXT')).toBeDisabled()

    fireEvent.change(screen.getByPlaceholderText('e.g. tante-laptop'), { target: { value: 'tante-laptop' } })
    expect(screen.getByText('NEXT')).not.toBeDisabled()
  })

  it('walks name -> OS -> hand-over and defaults to Windows', () => {
    renderWizard(<Wizard open onClose={vi.fn()} />)
    goToStep2()
    expect(screen.getByText('WINDOWS')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('LINUX')).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(screen.getByText('LINUX'))
    expect(screen.getByText('LINUX')).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(screen.getByText('NEXT'))
    expect(screen.getByText('Hand it over')).toBeInTheDocument()
  })

  it('share-link posts {name, os} to /api/agents/share-link and shows the returned URL', async () => {
    apiPostMock.mockResolvedValue({ url: 'https://kenny.local/o/abc123', expires_at: '2026-08-18T00:00:00Z', os: 'windows', name: 'tante-laptop' })

    renderWizard(<Wizard open onClose={vi.fn()} />)
    goToStep2()
    fireEvent.click(screen.getByText('NEXT'))
    fireEvent.click(screen.getByText('Share a one-time link'))

    await waitFor(() => expect(apiPostMock).toHaveBeenCalledWith('/api/agents/share-link', { name: 'tante-laptop', os: 'windows' }))
    expect(await screen.findByDisplayValue('https://kenny.local/o/abc123')).toBeInTheDocument()
  })

  it('is an ordinary dismissible modal — closes on Escape, unlike the chat confirm gate', () => {
    const onClose = vi.fn()
    renderWizard(<Wizard open onClose={onClose} />)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })

  it('renders nothing when closed', () => {
    renderWizard(<Wizard open={false} onClose={vi.fn()} />)
    expect(screen.queryByText('ADD A PC')).not.toBeInTheDocument()
  })
})

/**
 * The wizard is where a machine gets provisioned, so it has to know what the
 * server actually has staged. It read none of `/api/agent-binary` through 2.2.0:
 * no target list, no availability check, and the Linux install command dropped on
 * the floor. The download is a page navigation, so an unavailable binary did not
 * surface as a caught error — it replaced the wizard with the route's 503 JSON.
 */
describe('Wizard — what the server has staged', () => {
  function atOsStep() {
    renderWizard(<Wizard open onClose={vi.fn()} />)
    goToStep2()
  }

  it('offers only architectures a binary exists for', async () => {
    atOsStep()
    fireEvent.click(screen.getByText('LINUX'))

    const select = await screen.findByLabelText('Processor architecture')
    const offered = [...select.querySelectorAll('option')].map((o) => o.textContent)
    expect(offered).toEqual(['Detect on the machine', 'x86_64', 'aarch64'])
  })

  it('offers no architecture choice where there is only one target to choose', async () => {
    atOsStep()
    // Windows publishes a single target, so a select of one would be noise.
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/agent-binary'))
    expect(screen.queryByLabelText('Processor architecture')).not.toBeInTheDocument()
  })

  it('pins the chosen architecture onto the share link', async () => {
    apiPostMock.mockResolvedValue({ url: 'https://kenny.local/o/abc', expires_at: '2026-08-18T00:00:00Z', os: 'linux', name: 'tante-laptop' })
    atOsStep()
    fireEvent.click(screen.getByText('LINUX'))

    fireEvent.change(await screen.findByLabelText('Processor architecture'), { target: { value: 'aarch64' } })
    fireEvent.click(screen.getByText('NEXT'))
    fireEvent.click(screen.getByText('Share a one-time link'))

    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/agents/share-link', {
        name: 'tante-laptop',
        os: 'linux',
        arch: 'aarch64',
      }),
    )
  })

  it('blocks the download with a reason when nothing is staged for the chosen OS', async () => {
    apiGetMock.mockResolvedValue({
      ...BINARY_OK,
      available: false,
      by_os: { windows: false, linux: true },
      targets: [
        { os: 'windows', arch: 'x86_64', available: false },
        { os: 'linux', arch: 'x86_64', available: true },
      ],
    })
    atOsStep()

    expect(await screen.findByText(/No Windows agent binary is staged/)).toBeInTheDocument()
    fireEvent.click(screen.getByText('NEXT'))
    expect(screen.getByText('Download installer').closest('button')).toBeDisabled()
  })

  it('leaves provisioning alone when the status read itself fails', async () => {
    apiGetMock.mockRejectedValue(new Error('boom'))
    atOsStep()
    fireEvent.click(screen.getByText('NEXT'))

    // Not knowing is not the same as knowing there is nothing.
    await waitFor(() => expect(screen.getByText('Download installer').closest('button')).not.toBeDisabled())
  })

  it('hands over the Linux install command, not just the URL', async () => {
    apiPostMock.mockResolvedValue({
      url: 'https://kenny.local/d/install/abc',
      oneliner: 'curl -fsSL https://kenny.local/d/install/abc | sudo sh',
      expires_at: '2026-08-18T00:00:00Z',
      os: 'linux',
      name: 'tante-laptop',
    })
    atOsStep()
    fireEvent.click(screen.getByText('LINUX'))
    fireEvent.click(screen.getByText('NEXT'))
    fireEvent.click(screen.getByText('Share a one-time link'))

    // The URL alone is not usable — it has to be piped to a root shell.
    expect(
      await screen.findByDisplayValue('curl -fsSL https://kenny.local/d/install/abc | sudo sh'),
    ).toBeInTheDocument()
  })

  it('shows no install command for a Windows link, which has none', async () => {
    apiPostMock.mockResolvedValue({ url: 'https://kenny.local/d/installer/abc', expires_at: '2026-08-18T00:00:00Z', os: 'windows', name: 'tante-laptop' })
    atOsStep()
    fireEvent.click(screen.getByText('NEXT'))
    fireEvent.click(screen.getByText('Share a one-time link'))

    await screen.findByDisplayValue('https://kenny.local/d/installer/abc')
    expect(screen.queryByLabelText('Install one-liner')).not.toBeInTheDocument()
  })
})

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { apiGetMock, apiPostMock } = vi.hoisted(() => ({ apiGetMock: vi.fn(), apiPostMock: vi.fn() }))
vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  api: { get: apiGetMock, post: apiPostMock, put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('../../api/sse', () => ({ streamChatEvents: vi.fn() }))

const { chatStore } = await import('../../chat/chatStore')
const { default: AskKennyDrawer } = await import('./AskKennyDrawer')
const { default: StewardQuickActions } = await import('./StewardQuickActions')

const COMMANDS = {
  host_id: 'study-pc',
  stewards: [
    { steward_id: 'patch', title: 'Package updates', summary: 'Keeps allowed packages current.', mode: 'act', preview_allowed: true, run_allowed: true },
    { steward_id: 'disk', title: 'Disk space', summary: 'Frees space.', mode: 'shadow', preview_allowed: true, run_allowed: false },
  ],
}

function serve(commands: unknown = COMMANDS) {
  apiGetMock.mockImplementation((path: string) => {
    if (path.startsWith('/api/stewards/commands')) {
      return commands instanceof Error ? Promise.reject(commands) : Promise.resolve(commands)
    }
    if (path === '/api/stewards') return Promise.resolve({ stewards: [{ id: 'patch', title: 'Package updates' }] })
    if (path.includes('/command')) return Promise.reject(new Error('not needed here'))
    return Promise.resolve({ conversations: [] })
  })
}

beforeEach(() => {
  window.location.hash = ''
  chatStore.reset('')
})
afterEach(() => {
  cleanup()
  apiGetMock.mockReset()
  apiPostMock.mockReset()
})

describe('StewardQuickActions', () => {
  it('lists the stewards the server offers for this PC and starts nothing when one is chosen', async () => {
    serve()
    const onPick = vi.fn()
    render(<StewardQuickActions hostId="study-pc" onPick={onPick} />)
    expect(await screen.findByRole('button', { name: /Package updates/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Disk space/ })).toBeInTheDocument()
    expect(apiGetMock).toHaveBeenCalledWith('/api/stewards/commands?host_id=study-pc')

    fireEvent.click(screen.getByRole('button', { name: /Package updates/ }))
    expect(onPick).toHaveBeenCalledWith('patch', 'study-pc', expect.any(String))
    expect(apiPostMock).not.toHaveBeenCalled()
  })

  it('shows nothing when no steward qualifies or the list cannot be read', async () => {
    serve({ host_id: 'study-pc', stewards: [] })
    const { container, unmount } = render(<StewardQuickActions hostId="study-pc" onPick={vi.fn()} />)
    await waitFor(() => expect(apiGetMock).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
    unmount()

    serve(new Error('offline'))
    const second = render(<StewardQuickActions hostId="study-pc" onPick={vi.fn()} />)
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledTimes(2))
    expect(second.container).toBeEmptyDOMElement()
  })
})

describe('AskKennyDrawer — quick actions', () => {
  it('on a host page, the empty conversation offers that PC’s stewards; choosing one adds a pending card and starts no run', async () => {
    serve()
    window.location.hash = '#/fleet/study-pc'
    render(<AskKennyDrawer />)

    fireEvent.click(await screen.findByRole('button', { name: /Package updates/ }))

    expect(await screen.findByText(/PROPOSED RUN — nothing starts until you press a button/)).toBeInTheDocument()
    expect(chatStore.getState().items).toMatchObject([
      { kind: 'steward_proposal', stewardId: 'patch', hostId: 'study-pc', resolution: 'pending' },
    ])
    // The empty state (and with it the list) has made way for the card.
    expect(screen.queryByTestId('steward-quick-actions')).toBeNull()
    expect(apiPostMock).not.toHaveBeenCalled()
  })

  it('offers nothing on a fleet-wide conversation', async () => {
    serve()
    render(<AskKennyDrawer />)
    await screen.findByText(/Ask kenny anything about this fleet/)
    expect(apiGetMock).not.toHaveBeenCalledWith(expect.stringContaining('/api/stewards/commands'))
    expect(screen.queryByTestId('steward-quick-actions')).toBeNull()
  })
})

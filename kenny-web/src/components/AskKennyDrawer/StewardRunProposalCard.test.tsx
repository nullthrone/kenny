import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const { apiGetMock, apiPostMock } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  apiPostMock: vi.fn(),
}))
vi.mock('../../api/client', () => ({
  api: { get: apiGetMock, post: apiPostMock },
}))
vi.mock('../../api/sse', () => ({ streamChatEvents: vi.fn() }))

const { default: StewardRunProposalCard } = await import('./StewardRunProposalCard')
const { chatStore } = await import('../../chat/chatStore')
const { applyChatEvent } = await import('../../chat/reducer')

afterEach(() => {
  cleanup()
  apiGetMock.mockReset()
  apiPostMock.mockReset()
})

const PROPOSAL = {
  itemId: 'item-4',
  stewardId: 'patch',
  hostId: 'study-pc',
  reason: 'Three packages are behind on study-pc.',
}

function listStewards() {
  apiGetMock.mockResolvedValue({ stewards: [{ id: 'patch', title: 'Package updates' }] })
}

describe('StewardRunProposalCard', () => {
  it('shows the steward title, host, reason and that a preview changes nothing — and starts nothing itself', async () => {
    listStewards()
    const onStart = vi.fn()
    render(<StewardRunProposalCard {...PROPOSAL} resolution="pending" onStart={onStart} onDismiss={vi.fn()} />)

    expect(await screen.findByText('Package updates')).toBeInTheDocument()
    expect(screen.getByText('study-pc')).toBeInTheDocument()
    expect(screen.getByText(PROPOSAL.reason)).toBeInTheDocument()
    expect(screen.getByText(/A preview never changes anything/)).toBeInTheDocument()
    expect(onStart).not.toHaveBeenCalled()
    expect(apiPostMock).not.toHaveBeenCalled()
  })

  it('falls back to the steward id when the list cannot be read', async () => {
    apiGetMock.mockRejectedValue(new Error('offline'))
    render(<StewardRunProposalCard {...PROPOSAL} resolution="pending" onStart={vi.fn()} onDismiss={vi.fn()} />)
    expect(await screen.findByText('patch')).toBeInTheDocument()
  })

  it('starts the preview only when the button is pressed', async () => {
    listStewards()
    const onStart = vi.fn(() => Promise.resolve())
    render(<StewardRunProposalCard {...PROPOSAL} resolution="pending" onStart={onStart} onDismiss={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'START PREVIEW' }))
    await waitFor(() => expect(onStart).toHaveBeenCalledWith('item-4', 'patch', 'study-pc'))
  })

  it('shows the server’s text when the start is refused, and stays startable', async () => {
    listStewards()
    const onStart = vi.fn(() => Promise.reject(new Error('Stewards are switched off.')))
    render(<StewardRunProposalCard {...PROPOSAL} resolution="pending" onStart={onStart} onDismiss={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'START PREVIEW' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Stewards are switched off.')
    expect(screen.getByRole('button', { name: 'START PREVIEW' })).toBeEnabled()
  })

  it('puts the offer away without starting anything', () => {
    listStewards()
    const onStart = vi.fn()
    const onDismiss = vi.fn()
    render(<StewardRunProposalCard {...PROPOSAL} resolution="pending" onStart={onStart} onDismiss={onDismiss} />)
    fireEvent.click(screen.getByRole('button', { name: 'DISMISS' }))
    expect(onDismiss).toHaveBeenCalledWith('item-4')
    expect(onStart).not.toHaveBeenCalled()
  })

  it('stops being a card once started, and links the run to its steward', async () => {
    listStewards()
    render(
      <StewardRunProposalCard
        {...PROPOSAL}
        resolution="started"
        runId="run-77"
        onStart={vi.fn()}
        onDismiss={vi.fn()}
      />,
    )
    expect(screen.queryByRole('button', { name: 'START PREVIEW' })).toBeNull()
    expect(screen.getByText('run-77')).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: 'Package updates' })).toHaveAttribute('href', '#/admin/stewards/patch')
  })
})

describe('chatStore.startStewardRun', () => {
  function seed(hostId: string) {
    chatStore.reset('')
    chatStore['update']((st) =>
      applyChatEvent(st, { type: 'steward_run_proposal', steward_id: 'patch', host_id: hostId, reason: 'why' }),
    )
    return chatStore.getState().items[0].id
  }

  it('posts the host and records the returned run id on the card', async () => {
    const id = seed('study-pc')
    apiPostMock.mockResolvedValue({ run_id: 'run-9' })
    await chatStore.startStewardRun(id, 'patch', 'study-pc')
    expect(apiPostMock).toHaveBeenCalledWith('/api/stewards/patch/runs', { host_id: 'study-pc' })
    expect(chatStore.getState().items[0]).toMatchObject({ resolution: 'started', runId: 'run-9' })
  })

  it('posts an empty body for a host-less steward', async () => {
    const id = seed('')
    apiPostMock.mockResolvedValue({ run_id: 'run-10' })
    await chatStore.startStewardRun(id, 'patch', '')
    expect(apiPostMock).toHaveBeenCalledWith('/api/stewards/patch/runs', {})
  })

  it('leaves the card pending and rethrows the server’s error', async () => {
    const id = seed('study-pc')
    apiPostMock.mockRejectedValue(new Error('host is not one of this steward’s hosts'))
    await expect(chatStore.startStewardRun(id, 'patch', 'study-pc')).rejects.toThrow('host is not one of')
    expect(chatStore.getState().items[0]).toMatchObject({ resolution: 'pending' })
  })
})

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

const { default: AgentRunProposalCard } = await import('./AgentRunProposalCard')
const { chatStore } = await import('../../chat/chatStore')
const { applyChatEvent } = await import('../../chat/reducer')

afterEach(() => {
  cleanup()
  apiGetMock.mockReset()
  apiPostMock.mockReset()
})

const PROPOSAL = {
  itemId: 'item-4',
  agentId: 'patch',
  hostId: 'study-pc',
  reason: 'Three packages are behind on study-pc.',
}

function listAgents() {
  apiGetMock.mockResolvedValue({ agents: [{ id: 'patch', title: 'Package updates' }] })
}

describe('AgentRunProposalCard', () => {
  it('shows the agent title, host, reason and that a preview changes nothing — and starts nothing itself', async () => {
    listAgents()
    const onStart = vi.fn()
    render(<AgentRunProposalCard {...PROPOSAL} resolution="pending" onStart={onStart} onDismiss={vi.fn()} />)

    expect(await screen.findByText('Package updates')).toBeInTheDocument()
    expect(screen.getByText('study-pc')).toBeInTheDocument()
    expect(screen.getByText(PROPOSAL.reason)).toBeInTheDocument()
    expect(screen.getByText(/A preview never changes anything/)).toBeInTheDocument()
    expect(onStart).not.toHaveBeenCalled()
    expect(apiPostMock).not.toHaveBeenCalled()
  })

  it('falls back to the agent id when the list cannot be read', async () => {
    apiGetMock.mockRejectedValue(new Error('offline'))
    render(<AgentRunProposalCard {...PROPOSAL} resolution="pending" onStart={vi.fn()} onDismiss={vi.fn()} />)
    expect(await screen.findByText('patch')).toBeInTheDocument()
  })

  it('starts the preview only when the button is pressed', async () => {
    listAgents()
    const onStart = vi.fn(() => Promise.resolve())
    render(<AgentRunProposalCard {...PROPOSAL} resolution="pending" onStart={onStart} onDismiss={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'START PREVIEW' }))
    await waitFor(() => expect(onStart).toHaveBeenCalledWith('item-4', 'patch', 'study-pc'))
  })

  it('shows the server’s text when the start is refused, and stays startable', async () => {
    listAgents()
    const onStart = vi.fn(() => Promise.reject(new Error('Specialized agents are switched off.')))
    render(<AgentRunProposalCard {...PROPOSAL} resolution="pending" onStart={onStart} onDismiss={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'START PREVIEW' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Specialized agents are switched off.')
    expect(screen.getByRole('button', { name: 'START PREVIEW' })).toBeEnabled()
  })

  it('puts the offer away without starting anything', () => {
    listAgents()
    const onStart = vi.fn()
    const onDismiss = vi.fn()
    render(<AgentRunProposalCard {...PROPOSAL} resolution="pending" onStart={onStart} onDismiss={onDismiss} />)
    fireEvent.click(screen.getByRole('button', { name: 'DISMISS' }))
    expect(onDismiss).toHaveBeenCalledWith('item-4')
    expect(onStart).not.toHaveBeenCalled()
  })

  it('stops being a card once started, and links the run to its agent', async () => {
    listAgents()
    render(
      <AgentRunProposalCard
        {...PROPOSAL}
        resolution="started"
        runId="run-77"
        onStart={vi.fn()}
        onDismiss={vi.fn()}
      />,
    )
    expect(screen.queryByRole('button', { name: 'START PREVIEW' })).toBeNull()
    expect(screen.getByText('run-77')).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: 'Package updates' })).toHaveAttribute('href', '#/admin/agents/patch')
  })
})

describe('chatStore.startAgentRun', () => {
  function seed(hostId: string) {
    chatStore.reset('')
    chatStore['update']((st) =>
      applyChatEvent(st, { type: 'agent_run_proposal', agent_id: 'patch', host_id: hostId, reason: 'why' }),
    )
    return chatStore.getState().items[0].id
  }

  it('posts the host and records the returned run id on the card', async () => {
    const id = seed('study-pc')
    apiPostMock.mockResolvedValue({ run_id: 'run-9' })
    await chatStore.startAgentRun(id, 'patch', 'study-pc')
    expect(apiPostMock).toHaveBeenCalledWith('/api/specialized-agents/patch/runs', { host_id: 'study-pc' })
    expect(chatStore.getState().items[0]).toMatchObject({ resolution: 'started', runId: 'run-9' })
  })

  it('posts an empty body for a host-less agent', async () => {
    const id = seed('')
    apiPostMock.mockResolvedValue({ run_id: 'run-10' })
    await chatStore.startAgentRun(id, 'patch', '')
    expect(apiPostMock).toHaveBeenCalledWith('/api/specialized-agents/patch/runs', {})
  })

  it('leaves the card pending and rethrows the server’s error', async () => {
    const id = seed('study-pc')
    apiPostMock.mockRejectedValue(new Error('host is not one of this agent’s hosts'))
    await expect(chatStore.startAgentRun(id, 'patch', 'study-pc')).rejects.toThrow('host is not one of')
    expect(chatStore.getState().items[0]).toMatchObject({ resolution: 'pending' })
  })
})

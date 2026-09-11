import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// `vi.mock` factories are hoisted above every other statement in the file,
// so any variable they close over must itself be created inside
// `vi.hoisted()` — a bare `const streamChatEventsMock = vi.fn()` above this
// would still throw "Cannot access before initialization".
const { streamChatEventsMock } = vi.hoisted(() => ({ streamChatEventsMock: vi.fn() }))

vi.mock('../../api/sse', () => ({
  streamChatEvents: (...args: unknown[]) => streamChatEventsMock(...args),
}))

vi.mock('../../api/client', () => ({
  api: {
    get: vi.fn(() => Promise.resolve({ conversations: [] })),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}))

// Imported after the mocks above so chatStore picks up the mocked modules.
const { chatStore } = await import('../../chat/chatStore')
const { default: AskKennyDrawer } = await import('./AskKennyDrawer')

function sendMessage(text: string) {
  fireEvent.change(screen.getByLabelText('Message kenny'), { target: { value: text } })
  fireEvent.click(screen.getByLabelText('Send'))
}

beforeEach(() => {
  window.location.hash = ''
  chatStore.reset('')
  streamChatEventsMock.mockReset()
})

describe('AskKennyDrawer — composer lock', () => {
  it('locks the composer once a pending event arrives — the gate, not a client-side guess', async () => {
    streamChatEventsMock.mockImplementation(async function* () {
      yield { type: 'pending', tool: 'powershell_exec', args: { script: 'Move-Item C:\\Videos D:\\Videos' }, agent_id: '' }
    })

    render(<AskKennyDrawer />)
    sendMessage('free up disk space')

    const textarea = await screen.findByLabelText<HTMLTextAreaElement>('Message kenny')
    await waitFor(() => expect(textarea).toBeDisabled())
    expect(textarea.placeholder).toBe('Waiting on the confirmation above…')
    expect(screen.getByLabelText('Send')).toBeDisabled()
  })

  it('a read-only tool_result never locks the composer', async () => {
    streamChatEventsMock.mockImplementation(async function* () {
      yield { type: 'tool_result', tool: 'fs_disk_usage', ok: true, auto_run: true }
      yield { type: 'done' }
    })

    render(<AskKennyDrawer />)
    sendMessage('what is using disk space?')

    await waitFor(() => expect(document.body.textContent).toContain('fs_disk_usage'))
    expect(screen.getByLabelText<HTMLTextAreaElement>('Message kenny')).not.toBeDisabled()
  })
})

describe('AskKennyDrawer — agent_id', () => {
  it('sends agent_id as "" (present, not omitted) when opened unscoped', async () => {
    streamChatEventsMock.mockImplementation(async function* () {
      yield { type: 'done' }
    })

    render(<AskKennyDrawer />)
    sendMessage('hello')

    await waitFor(() => expect(streamChatEventsMock).toHaveBeenCalled())
    const [url, body] = streamChatEventsMock.mock.calls[0] as [string, Record<string, unknown>]
    expect(url).toBe('/api/chat/stream')
    expect(body).toHaveProperty('agent_id', '')
    expect(body).toMatchObject({ scope: 'fleet' })
  })

  it('sends the host as agent_id when opened from that host page', async () => {
    window.location.hash = '#/fleet/oma-pc'
    chatStore.reset('oma-pc')
    streamChatEventsMock.mockImplementation(async function* () {
      yield { type: 'done' }
    })

    render(<AskKennyDrawer />)
    sendMessage('what is wrong with this pc?')

    await waitFor(() => expect(streamChatEventsMock).toHaveBeenCalled())
    const [, body] = streamChatEventsMock.mock.calls[0] as [string, Record<string, unknown>]
    expect(body).toMatchObject({ agent_id: 'oma-pc', scope: 'host' })
  })
})

describe('AskKennyDrawer — the confirm gate is non-dismissible', () => {
  it('does not close the gate modal on Escape', async () => {
    streamChatEventsMock.mockImplementation(async function* () {
      yield { type: 'pending', tool: 'powershell_exec', args: {}, agent_id: '' }
    })

    render(<AskKennyDrawer />)
    sendMessage('do the risky thing')

    await waitFor(() => expect(screen.getByText('CONFIRM & RUN')).toBeInTheDocument())

    fireEvent.keyDown(window, { key: 'Escape' })

    expect(screen.getByText('CONFIRM & RUN')).toBeInTheDocument()
    expect(screen.getByText('CANCEL')).toBeInTheDocument()
  })

  it('has no close cross and no click-outside handler on its own backdrop', async () => {
    streamChatEventsMock.mockImplementation(async function* () {
      yield { type: 'pending', tool: 'powershell_exec', args: {}, agent_id: '' }
    })

    render(<AskKennyDrawer />)
    sendMessage('do the risky thing')

    await waitFor(() => expect(screen.getByText('CONFIRM & RUN')).toBeInTheDocument())
    expect(screen.queryByText('DECIDE LATER')).not.toBeInTheDocument()

    const dialog = screen.getByRole('dialog')
    // Its only exits are the two GateCard buttons.
    const buttons = dialog.querySelectorAll('button')
    expect(Array.from(buttons).map((b) => b.textContent)).toEqual(['CONFIRM & RUN', 'CANCEL'])
  })

  it('only resolves through CONFIRM & RUN / CANCEL, each posting to the confirm stream', async () => {
    streamChatEventsMock.mockImplementation(async function* (url: string) {
      if (url === '/api/chat/stream') {
        yield { type: 'pending', tool: 'powershell_exec', args: {}, agent_id: '' }
      } else {
        yield { type: 'denied', tool: 'powershell_exec' }
        yield { type: 'done' }
      }
    })

    render(<AskKennyDrawer />)
    sendMessage('do the risky thing')

    await waitFor(() => expect(screen.getByText('CANCEL')).toBeInTheDocument())
    fireEvent.click(screen.getByText('CANCEL'))

    await waitFor(() => expect(screen.queryByText('CONFIRM & RUN')).not.toBeInTheDocument())
    const confirmCall = streamChatEventsMock.mock.calls.find(([url]) => url === '/api/chat/confirm/stream')
    expect(confirmCall?.[1]).toMatchObject({ approve: false })
  })
})

describe('AskKennyDrawer — a ticket is the second context, not a second drawer', () => {
  const TICKET = {
    id: 't-42',
    number: 76,
    agentId: 'linus-pc',
    discordThread: false,
    assistantAvailable: true,
    blockedOnApproval: false,
  }

  function openOnTicket(over: Partial<typeof TICKET> = {}) {
    window.location.hash = '#/inbox/ticket/t-42'
    // The ticket page binds the target before the drawer is ever opened; the
    // drawer reads it and never fetches, which is why it needs no client.
    chatStore.openForTicket({ ...TICKET, ...over })
    return render(<AskKennyDrawer />)
  }

  it('posts to the ticket, and sends no agent_id at all', async () => {
    // The host is the ticket's frozen `agent_id` and nothing said in the
    // conversation may move it (ADR-0038), so there is nothing to send.
    streamChatEventsMock.mockImplementation(async function* () {
      yield { type: 'done' }
    })

    openOnTicket()
    sendMessage('what is filling the disk?')

    await waitFor(() => expect(streamChatEventsMock).toHaveBeenCalled())
    const [url, body] = streamChatEventsMock.mock.calls[0] as [string, Record<string, unknown>]
    expect(url).toBe('/api/tickets/t-42/chat/stream')
    expect(body).toEqual({ message: 'what is filling the disk?', mirror_to_discord: false })
  })

  it('names the ticket it is talking about, so the gate in force is readable', () => {
    openOnTicket()
    expect(screen.getByText('ticket #76 · linus-pc')).toBeInTheDocument()
  })

  it('offers no conversation history — the ticket timeline is its history', () => {
    openOnTicket()
    expect(screen.queryByTitle('History')).not.toBeInTheDocument()
    expect(screen.queryByTitle('New conversation')).not.toBeInTheDocument()
  })

  it('does not decide a ticket gate here; it points at where the evidence is', async () => {
    // A ticket's gate is durable and is answered beside the frozen call it
    // would run (ADR-0050). A second CONFIRM in this drawer would be a
    // decision offered without what it decides.
    streamChatEventsMock.mockImplementation(async function* () {
      yield { type: 'pending', tool: 'powershell_exec', args: {}, agent_id: 'linus-pc' }
    })

    openOnTicket()
    sendMessage('clean it up')

    const textarea = await screen.findByLabelText<HTMLTextAreaElement>('Message kenny')
    await waitFor(() => expect(textarea).toBeDisabled())
    expect(screen.queryByText('CONFIRM & RUN')).not.toBeInTheDocument()
    expect(document.body.textContent).toContain('It is on the ticket')
  })

  it('offers the Discord mirror only when the ticket has a thread', () => {
    const label = 'Also send to the Discord thread'
    openOnTicket()
    expect(screen.queryByLabelText(label)).not.toBeInTheDocument()

    cleanup()
    chatStore.reset('')
    openOnTicket({ discordThread: true })
    expect(screen.getByLabelText(label)).toBeInTheDocument()
  })

  it('says why it cannot be used rather than failing on send', () => {
    cleanup()
    chatStore.reset('')
    openOnTicket({ assistantAvailable: false })
    expect(screen.getByLabelText<HTMLTextAreaElement>('Message kenny').placeholder).toBe(
      'The AI assistant is not configured on this server.',
    )
  })

  it('never falls back to fleet chat on a ticket route it is not bound to', () => {
    // The failure this guards against is silent: a turn would run on the
    // copilot's endpoint, under the copilot's gate, while the reader believed
    // they were talking about the ticket.
    cleanup()
    chatStore.reset('')
    window.location.hash = '#/inbox/ticket/t-99'
    render(<AskKennyDrawer />)

    expect(screen.queryByLabelText('Message kenny')).not.toBeInTheDocument()
    expect(screen.getByText('opening this ticket…')).toBeInTheDocument()
  })
})

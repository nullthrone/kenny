import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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

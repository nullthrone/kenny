import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const { apiGetMock, apiPostMock } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  apiPostMock: vi.fn(),
}))
// The real ApiError stays: the refusal wording branches on what the client throws.
vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  api: { get: apiGetMock, post: apiPostMock },
}))
vi.mock('../../api/sse', () => ({ streamChatEvents: vi.fn() }))

const { ApiError } = await import('../../api/client')
const { default: StewardRunProposalCard } = await import('./StewardRunProposalCard')
const { RUN_POLL_MAX_MS, RUN_POLL_MS } = await import('./useStewardRunStatus')
const { chatStore, RESULT_RETRY_MS } = await import('../../chat/chatStore')
const { applyChatEvent } = await import('../../chat/reducer')

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  apiGetMock.mockReset()
  apiPostMock.mockReset()
})

const PROPOSAL = {
  itemId: 'item-4',
  stewardId: 'patch',
  hostId: 'study-pc',
  reason: 'Three packages are behind on study-pc.',
}

const HASH = 'a'.repeat(64)

function readiness(over: Record<string, unknown> = {}) {
  return {
    steward_id: 'patch',
    host_id: 'study-pc',
    effective_hash: HASH,
    mode: 'act',
    preview: { allowed: true, reason: null },
    run: { allowed: true, reason: null, code: null },
    tools: [
      {
        tool: 'winget_update',
        tier: 'normal_change',
        values: { package_id: ['Mozilla.Firefox', 'VideoLAN.VLC'] },
        needs_authorization: true,
        authorization: { id: 'auth-1', remaining_today: 2, expires_at: '2026-11-30T00:00:00+00:00' },
      },
      {
        tool: 'fs_cleanup',
        tier: 'normal_change',
        values: { target: 'computed at run start' },
        needs_authorization: true,
        authorization: null,
      },
    ],
    ...over,
  }
}

/** `GET`s the card makes, by path. A run's status is read from `runs`, in order, the last one repeating. */
function serve(opts: { ready?: unknown; runs?: unknown[] } = {}) {
  const runs = [...(opts.runs ?? [])]
  apiGetMock.mockImplementation((path: string) => {
    if (path === '/api/stewards') return Promise.resolve({ stewards: [{ id: 'patch', title: 'Package updates' }] })
    if (path.startsWith('/api/stewards/patch/command')) {
      return opts.ready instanceof Error ? Promise.reject(opts.ready) : Promise.resolve(opts.ready ?? readiness())
    }
    if (path.startsWith('/api/stewards/runs/')) {
      return Promise.resolve(runs.length > 1 ? runs.shift() : runs[0])
    }
    return Promise.reject(new Error(`unexpected GET ${path}`))
  })
}

function run(over: Record<string, unknown> = {}) {
  return {
    id: 'run-77',
    steward_id: 'patch',
    host_id: 'study-pc',
    mode: 'act',
    status: 'running',
    verdict: null,
    actions: [],
    recommendations: [],
    ...over,
  }
}

function card(over: Partial<React.ComponentProps<typeof StewardRunProposalCard>> = {}) {
  const handlers = {
    onStart: vi.fn(() => Promise.resolve()),
    onRunNow: vi.fn(() => Promise.resolve()),
    onFinished: vi.fn(),
    onDismiss: vi.fn(),
  }
  const utils = render(<StewardRunProposalCard {...PROPOSAL} resolution="pending" {...handlers} {...over} />)
  return { ...utils, ...handlers }
}

describe('StewardRunProposalCard — the copilot’s reason is shown as the copilot’s words', () => {
  it('labels the reason as Kenny’s suggestion, quotes it, and renders it as text rather than markup', async () => {
    serve()
    const hostile = '<img src=x onerror=alert(1)> <b>run it</b>'
    const { container } = card({ reason: hostile })
    await screen.findByText('Package updates')
    expect(screen.getByText('KENNY SUGGESTS')).toBeInTheDocument()
    expect(screen.queryByText('WHY')).toBeNull()
    const quoted = container.querySelector('q')!
    expect(quoted.textContent).toBe(hostile)
    expect(quoted.className).toMatch(/suggested/)
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('dd b')).toBeNull()
  })

  it('shows no suggestion row for a run the operator picked themselves', async () => {
    serve()
    card({ reason: '' })
    await screen.findByText('Package updates')
    expect(screen.queryByText('KENNY SUGGESTS')).toBeNull()
  })
})

describe('StewardRunProposalCard — what it shows before anything starts', () => {
  it('shows steward, PC, mode, why and each tool it may change with its authorization — and starts nothing itself', async () => {
    serve()
    const { onStart, onRunNow } = card()

    expect(await screen.findByText('Package updates')).toBeInTheDocument()
    expect(await screen.findByText('act')).toBeInTheDocument() // mode
    expect(screen.getByText('study-pc')).toBeInTheDocument()
    expect(screen.getByText(PROPOSAL.reason)).toBeInTheDocument()
    expect(screen.getByText(/A preview never changes anything/)).toBeInTheDocument()

    const tools = screen.getAllByRole('listitem')
    expect(tools).toHaveLength(2)
    expect(tools[0]).toHaveTextContent('winget_update')
    expect(tools[0]).toHaveTextContent('normal_change')
    expect(tools[0]).toHaveTextContent('package_id: Mozilla.Firefox, VideoLAN.VLC')
    expect(tools[0]).toHaveTextContent('authorized · 2 left today · until 2026-11-30T00:00:00+00:00')
    expect(tools[1]).toHaveTextContent('target: computed at run start')
    expect(tools[1]).toHaveTextContent('needs a standing authorization')

    expect(apiGetMock).toHaveBeenCalledWith('/api/stewards/patch/command?host_id=study-pc')
    expect(onStart).not.toHaveBeenCalled()
    expect(onRunNow).not.toHaveBeenCalled()
    expect(apiPostMock).not.toHaveBeenCalled()
  })

  it('asks for a steward that is not bound to a PC without a host', async () => {
    serve({ ready: readiness({ host_id: null, tools: [] }) })
    card({ hostId: '' })
    expect(await screen.findByText(/no tool that changes anything/)).toBeInTheDocument()
    expect(apiGetMock).toHaveBeenCalledWith('/api/stewards/patch/command')
  })

  it('falls back to the steward id when the list cannot be read', async () => {
    apiGetMock.mockRejectedValue(new Error('offline'))
    card()
    expect(await screen.findByText('patch')).toBeInTheDocument()
  })

  it('keeps RUN NOW off, with the server’s reason, when the caller may not start a run that acts — PREVIEW stays', async () => {
    serve({
      ready: readiness({
        run: { allowed: false, reason: 'starting a run that may act takes a superuser signed in to the dashboard', code: 'not_a_superuser_at_a_browser' },
      }),
    })
    card()
    expect(await screen.findByText(/Run now unavailable: starting a run that may act takes a superuser/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'RUN NOW' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'PREVIEW' })).toBeEnabled()
  })

  it('says why PREVIEW is off when the server refuses one', async () => {
    serve({ ready: readiness({ preview: { allowed: false, reason: 'steward is off' } }) })
    card()
    expect(await screen.findByText(/Preview unavailable: steward is off/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'PREVIEW' })).toBeDisabled()
  })

  it('cannot run now when readiness could not be read, but a preview is still the server’s to decide', async () => {
    serve({ ready: new Error('boom') })
    card()
    expect(await screen.findByText(/Could not read whether this run may start/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'RUN NOW' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'PREVIEW' })).toBeEnabled()
  })
})

describe('StewardRunProposalCard — the two buttons', () => {
  it('PREVIEW starts a preview and nothing else', async () => {
    serve()
    const { onStart, onRunNow } = card()
    await screen.findByText('act')
    fireEvent.click(screen.getByRole('button', { name: 'PREVIEW' }))
    await waitFor(() => expect(onStart).toHaveBeenCalledWith('item-4', 'patch', 'study-pc'))
    expect(onRunNow).not.toHaveBeenCalled()
  })

  it('RUN NOW names the effective hash the card showed', async () => {
    serve()
    const { onStart, onRunNow } = card()
    await waitFor(() => expect(screen.getByRole('button', { name: 'RUN NOW' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'RUN NOW' }))
    await waitFor(() => expect(onRunNow).toHaveBeenCalledWith('item-4', 'patch', 'study-pc', HASH))
    expect(onStart).not.toHaveBeenCalled()
  })

  it('puts the offer away without starting anything', async () => {
    serve()
    const { onStart, onRunNow, onDismiss } = card()
    fireEvent.click(screen.getByRole('button', { name: 'DISMISS' }))
    expect(onDismiss).toHaveBeenCalledWith('item-4')
    expect(onStart).not.toHaveBeenCalled()
    expect(onRunNow).not.toHaveBeenCalled()
  })

  it('shows the server’s text when a preview is refused, and stays startable', async () => {
    serve()
    card({ onStart: vi.fn(() => Promise.reject(new ApiError('Stewards are switched off.', 409, 'stewards_off'))) })
    await screen.findByText('act')
    fireEvent.click(screen.getByRole('button', { name: 'PREVIEW' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Stewards are switched off.')
    expect(screen.getByRole('button', { name: 'PREVIEW' })).toBeEnabled()
  })

  it('403 on RUN NOW reads as the consent sentence, not the raw status', async () => {
    serve()
    card({ onRunNow: vi.fn(() => Promise.reject(new ApiError('starting a run that may act takes a superuser', 403, 'not_a_superuser_at_a_browser'))) })
    await waitFor(() => expect(screen.getByRole('button', { name: 'RUN NOW' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'RUN NOW' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Sign in to the dashboard as a superuser')
  })

  it('409 hash_stale says the steward changed, reloads what it may do and runs against the new hash', async () => {
    serve()
    const NEW_HASH = 'b'.repeat(64)
    const onRunNow = vi
      .fn()
      .mockRejectedValueOnce(new ApiError('steward patch changed since it was shown', 409, 'hash_stale'))
      .mockResolvedValue(undefined)
    card({ onRunNow })
    await waitFor(() => expect(screen.getByRole('button', { name: 'RUN NOW' })).toBeEnabled())
    serve({ ready: readiness({ effective_hash: NEW_HASH, tools: [] }) })
    fireEvent.click(screen.getByRole('button', { name: 'RUN NOW' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('The steward changed since this card was loaded')
    await screen.findByText(/no tool that changes anything/)
    fireEvent.click(screen.getByRole('button', { name: 'RUN NOW' }))
    await waitFor(() => expect(onRunNow).toHaveBeenLastCalledWith('item-4', 'patch', 'study-pc', NEW_HASH))
  })

  it('other refusals (the PC is in use) show the server’s own reason', async () => {
    serve()
    card({
      onRunNow: vi.fn(() =>
        Promise.reject(new ApiError('somebody is signed in to study-pc; this steward runs only when it is idle', 409, 'host_not_idle')),
      ),
    })
    await waitFor(() => expect(screen.getByRole('button', { name: 'RUN NOW' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'RUN NOW' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('somebody is signed in to study-pc')
  })
})

describe('StewardRunProposalCard — following a started run', () => {
  async function tick(ms: number) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms)
    })
  }

  function started(over: Partial<React.ComponentProps<typeof StewardRunProposalCard>> = {}) {
    return card({ resolution: 'started', runId: 'run-77', startedMode: 'act', ...over })
  }

  it('stops being a card, names what started, and links the run’s steward', async () => {
    serve({ runs: [run()] })
    started()
    expect(screen.queryByRole('button', { name: 'RUN NOW' })).toBeNull()
    expect(screen.getByText(/Run started/)).toBeInTheDocument()
    expect(screen.getByText('run-77')).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: 'Package updates' })).toHaveAttribute('href', '#/admin/stewards/patch')
    // A started card does not look up readiness again.
    expect(apiGetMock).not.toHaveBeenCalledWith(expect.stringContaining('/command'))
  })

  it('says "Preview started" for a preview', async () => {
    serve({ runs: [run({ mode: 'shadow' })] })
    started({ startedMode: 'preview' })
    expect(screen.getByText(/Preview started/)).toBeInTheDocument()
  })

  it('polls every 3 s, shows running then the verdict, counts and bytes freed, and stops at a final status', async () => {
    vi.useFakeTimers()
    serve({
      runs: [
        run({ actions: [{ tool: 'fs_cleanup', result: { bytes_freed: 1024 } }] }),
        run({
          status: 'completed',
          verdict: 'acted',
          actions: [
            { tool: 'fs_cleanup', result: { files_removed: 4, bytes_freed: 1024 } },
            { tool: 'fs_cleanup', result: { files_removed: 2, bytes_freed: 2048 } },
            { tool: 'winget_update', ok: true },
          ],
          recommendations: [{ tool: 'x' }],
        }),
      ],
    })
    const { onFinished } = started()
    const polls = () => apiGetMock.mock.calls.filter(([p]) => String(p).startsWith('/api/stewards/runs/')).length

    await tick(0)
    expect(polls()).toBe(1)
    expect(screen.getByRole('status')).toHaveTextContent('Running… 1 action so far · 1.0 KB freed')
    expect(onFinished).not.toHaveBeenCalled()

    await tick(RUN_POLL_MS)
    expect(polls()).toBe(2)
    expect(screen.getByRole('status')).toHaveTextContent(
      'Finished · verdict acted · 3 actions · 1 recommendation · 3.0 KB freed',
    )
    expect(onFinished).toHaveBeenCalledTimes(1)
    expect(onFinished).toHaveBeenCalledWith('item-4', 'run-77')

    await tick(RUN_POLL_MS * 10)
    expect(polls()).toBe(2) // stopped
    expect(onFinished).toHaveBeenCalledTimes(1)
  })

  it('a failed run is final too', async () => {
    vi.useFakeTimers()
    serve({ runs: [run({ status: 'failed' })] })
    const { onFinished } = started()
    await tick(0)
    expect(screen.getByRole('status')).toHaveTextContent('Failed · 0 actions · 0 recommendations')
    expect(onFinished).toHaveBeenCalledWith('item-4', 'run-77')
  })

  it('does not tell the conversation again for a result it already reported', async () => {
    vi.useFakeTimers()
    serve({ runs: [run({ status: 'completed', verdict: 'clean' })] })
    const { onFinished } = started({ resultReported: true })
    await tick(0)
    expect(screen.getByRole('status')).toHaveTextContent('Finished')
    expect(onFinished).not.toHaveBeenCalled()
  })

  it('gives up after about 15 minutes and says where to follow the run', async () => {
    vi.useFakeTimers()
    serve({ runs: [run()] })
    const { onFinished } = started()
    await tick(RUN_POLL_MAX_MS + RUN_POLL_MS)
    const polls = apiGetMock.mock.calls.filter(([p]) => String(p).startsWith('/api/stewards/runs/')).length
    expect(screen.getByRole('status')).toHaveTextContent('still going after 15 minutes')
    await tick(RUN_POLL_MS * 20)
    expect(apiGetMock.mock.calls.filter(([p]) => String(p).startsWith('/api/stewards/runs/')).length).toBe(polls)
    expect(onFinished).not.toHaveBeenCalled()
  })

  it('keeps trying when a read fails, and stops when the run is gone', async () => {
    vi.useFakeTimers()
    let reads = 0
    apiGetMock.mockImplementation((path: string) => {
      if (path === '/api/stewards') return Promise.resolve({ stewards: [] })
      reads += 1
      return reads === 1 ? Promise.reject(new Error('network')) : Promise.reject(new ApiError('run not found', 404))
    })
    started()
    await tick(0)
    expect(screen.getByRole('status')).toHaveTextContent('Could not read the run yet: network')
    await tick(RUN_POLL_MS)
    expect(reads).toBe(2)
    await tick(RUN_POLL_MS * 5)
    expect(reads).toBe(2)
  })

  it('stops reading when the card goes away', async () => {
    vi.useFakeTimers()
    serve({ runs: [run()] })
    const { unmount } = started()
    await tick(0)
    unmount()
    const before = apiGetMock.mock.calls.length
    await tick(RUN_POLL_MS * 5)
    expect(apiGetMock.mock.calls.length).toBe(before)
  })
})

describe('chatStore — the card’s start, run-now and result calls', () => {
  function seed(hostId: string, sessionId: string | null = 'sess-1') {
    chatStore.reset('')
    chatStore['update']((st) => ({
      ...applyChatEvent(st, { type: 'steward_run_proposal', steward_id: 'patch', host_id: hostId, reason: 'why' }),
      sessionId,
    }))
    return chatStore.getState().items[0].id
  }

  it('startStewardRun posts the host and records the run id on the card', async () => {
    const id = seed('study-pc')
    apiPostMock.mockResolvedValue({ run_id: 'run-9' })
    await chatStore.startStewardRun(id, 'patch', 'study-pc')
    expect(apiPostMock).toHaveBeenCalledWith('/api/stewards/patch/runs', { host_id: 'study-pc' })
    expect(chatStore.getState().items[0]).toMatchObject({ resolution: 'started', runId: 'run-9', startedMode: 'preview' })
  })

  it('startStewardRun posts an empty body for a host-less steward', async () => {
    const id = seed('')
    apiPostMock.mockResolvedValue({ run_id: 'run-10' })
    await chatStore.startStewardRun(id, 'patch', '')
    expect(apiPostMock).toHaveBeenCalledWith('/api/stewards/patch/runs', {})
  })

  it('startStewardRun leaves the card pending and rethrows the server’s error', async () => {
    const id = seed('study-pc')
    apiPostMock.mockRejectedValue(new Error('host is not one of this steward’s hosts'))
    await expect(chatStore.startStewardRun(id, 'patch', 'study-pc')).rejects.toThrow('host is not one of')
    expect(chatStore.getState().items[0]).toMatchObject({ resolution: 'pending' })
  })

  it('runStewardNow posts mode "act" with the host and the effective hash it was shown', async () => {
    const id = seed('study-pc')
    apiPostMock.mockResolvedValue({ run_id: 'run-11' })
    await chatStore.runStewardNow(id, 'patch', 'study-pc', HASH)
    expect(apiPostMock).toHaveBeenCalledWith('/api/stewards/patch/runs', {
      host_id: 'study-pc',
      mode: 'act',
      effective_hash: HASH,
    })
    expect(chatStore.getState().items[0]).toMatchObject({ resolution: 'started', runId: 'run-11', startedMode: 'act' })
  })

  it('runStewardNow leaves the card pending and rethrows the refusal with its code', async () => {
    const id = seed('study-pc')
    apiPostMock.mockRejectedValue(new ApiError('changed', 409, 'hash_stale'))
    await expect(chatStore.runStewardNow(id, 'patch', 'study-pc', HASH)).rejects.toMatchObject({
      status: 409,
      code: 'hash_stale',
    })
    expect(chatStore.getState().items[0]).toMatchObject({ resolution: 'pending' })
  })

  it('reportStewardRun tells the conversation once, naming only session and run', async () => {
    const id = seed('study-pc')
    apiPostMock.mockResolvedValue({ appended: true })
    await chatStore.reportStewardRun(id, 'run-9')
    await chatStore.reportStewardRun(id, 'run-9')
    expect(apiPostMock).toHaveBeenCalledTimes(1)
    expect(apiPostMock).toHaveBeenCalledWith('/api/chat/steward-run-result', { session_id: 'sess-1', run_id: 'run-9' })
    expect(chatStore.getState().items[0]).toMatchObject({ resultReported: true })
  })

  it('reportStewardRun has nothing to tell a conversation the server does not know yet', async () => {
    const id = seed('study-pc', null)
    await chatStore.reportStewardRun(id, 'run-9')
    expect(apiPostMock).not.toHaveBeenCalled()
    expect(chatStore.getState().items[0]).toMatchObject({ resultReported: true })
  })

  it('reportStewardRun asks again while the conversation is waiting on a confirmation, then lets go', async () => {
    vi.useFakeTimers()
    const id = seed('study-pc')
    apiPostMock
      .mockRejectedValueOnce(new ApiError('a confirmation is pending', 409, 'session_busy'))
      .mockResolvedValue({ appended: true })
    const done = chatStore.reportStewardRun(id, 'run-9')
    await vi.advanceTimersByTimeAsync(RESULT_RETRY_MS)
    await done
    expect(apiPostMock).toHaveBeenCalledTimes(2)
  })

  it('reportStewardRun does not retry a refusal that will not change', async () => {
    const id = seed('study-pc')
    apiPostMock.mockRejectedValue(new ApiError('only the person who started a run', 403, 'not_initiator'))
    await chatStore.reportStewardRun(id, 'run-9')
    expect(apiPostMock).toHaveBeenCalledTimes(1)
  })
})

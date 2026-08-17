import { describe, expect, it } from 'vitest'
import { applyChatEvent, startUserTurn } from './reducer'
import { makeInitialState } from './types'

describe('startUserTurn', () => {
  it('pushes a user bubble and marks the session streaming', () => {
    const s = startUserTurn(makeInitialState(''), 'free up disk space')
    expect(s.items).toEqual([{ kind: 'user', id: 'item-0', text: 'free up disk space' }])
    expect(s.streaming).toBe(true)
  })
})

describe('applyChatEvent — text_delta', () => {
  it('accumulates deltas into a single assistant item rather than appending separate ones', () => {
    let s = makeInitialState('')
    s = applyChatEvent(s, { type: 'text_delta', text: 'Drive C: holds ' })
    s = applyChatEvent(s, { type: 'text_delta', text: '412 GB' })
    expect(s.items).toEqual([{ kind: 'assistant', id: 'item-0', text: 'Drive C: holds 412 GB' }])
  })

  it('starts a fresh assistant item after an intervening tool_result', () => {
    let s = makeInitialState('')
    s = applyChatEvent(s, { type: 'text_delta', text: 'checking...' })
    s = applyChatEvent(s, { type: 'tool_result', tool: 'fs_disk_usage', ok: true, auto_run: true })
    s = applyChatEvent(s, { type: 'text_delta', text: 'done.' })
    const assistantTexts = s.items.filter((i) => i.kind === 'assistant').map((i) => (i as { text: string }).text)
    expect(assistantTexts).toEqual(['checking...', 'done.'])
  })
})

describe('applyChatEvent — tool_result (read-only, auto-run)', () => {
  it('renders as an auto-run chip, never as a gate — there is no client-side tool classification', () => {
    const s = applyChatEvent(makeInitialState(''), { type: 'tool_result', tool: 'fs_disk_usage', ok: true, auto_run: true })
    expect(s.items).toEqual([{ kind: 'auto_run', id: 'item-0', tool: 'fs_disk_usage', ok: true, imageB64: undefined, format: undefined }])
    expect(s.pendingGate).toBeNull()
  })
})

describe('applyChatEvent — pending', () => {
  it('is the gate: sets pendingGate, adds a gate transcript row, and the call has not run', () => {
    const s = applyChatEvent(makeInitialState('oma-pc'), {
      type: 'pending',
      tool: 'powershell_exec',
      args: { script: 'Move-Item C:\\Users\\oma\\Videos\\* D:\\Videos\\' },
      agent_id: 'oma-pc',
      tool_class: 'mutating',
    })
    expect(s.pendingGate).toEqual({
      itemId: 'item-0',
      tool: 'powershell_exec',
      args: { script: 'Move-Item C:\\Users\\oma\\Videos\\* D:\\Videos\\' },
      agentId: 'oma-pc',
      toolClass: 'mutating',
    })
    expect(s.items).toHaveLength(1)
    expect(s.items[0]).toMatchObject({ kind: 'gate', resolution: 'pending', tool: 'powershell_exec' })
    // The initial stream closes here — nothing is in flight until confirm/stream.
    expect(s.streaming).toBe(false)
  })

  it('never appears for a read-only call — tool_result and pending are mutually exclusive per event, not per tool', () => {
    let s = makeInitialState('')
    s = applyChatEvent(s, { type: 'tool_result', tool: 'fs_disk_usage', ok: true, auto_run: true })
    expect(s.pendingGate).toBeNull()
    expect(s.items.every((i) => i.kind !== 'gate')).toBe(true)
  })
})

describe('applyChatEvent — resolving a gate', () => {
  it('tool_result after a confirm updates the matching gate item in place, not a second row', () => {
    let s = makeInitialState('oma-pc')
    s = applyChatEvent(s, { type: 'pending', tool: 'powershell_exec', args: {}, agent_id: 'oma-pc' })
    // The store keeps pendingGate set (buttons disabled via `deciding`) until the
    // result actually lands — see chatStore.resolveGate.
    s = { ...s, deciding: true, resolvingGateItemId: s.pendingGate!.itemId }
    s = applyChatEvent(s, { type: 'tool_result', tool: 'powershell_exec', ok: true, auto_run: false })
    expect(s.items).toHaveLength(1)
    expect(s.items[0]).toMatchObject({ kind: 'gate', resolution: 'approved', ok: true })
    expect(s.resolvingGateItemId).toBeNull()
    expect(s.pendingGate).toBeNull()
    expect(s.deciding).toBe(false)
  })

  it('denied after a confirm(false) updates the matching gate item in place', () => {
    let s = makeInitialState('oma-pc')
    s = applyChatEvent(s, { type: 'pending', tool: 'powershell_exec', args: {}, agent_id: 'oma-pc' })
    s = { ...s, deciding: true, resolvingGateItemId: s.pendingGate!.itemId }
    s = applyChatEvent(s, { type: 'denied', tool: 'powershell_exec' })
    expect(s.items).toHaveLength(1)
    expect(s.items[0]).toMatchObject({ kind: 'gate', resolution: 'denied' })
    expect(s.pendingGate).toBeNull()
  })
})

describe('applyChatEvent — done/error', () => {
  it('done clears streaming and captures session_id', () => {
    const s = applyChatEvent({ ...makeInitialState(''), streaming: true }, { type: 'done', session_id: 'sess-1' })
    expect(s.streaming).toBe(false)
    expect(s.sessionId).toBe('sess-1')
  })

  it('error clears streaming and any pending gate, and surfaces an error item', () => {
    let s = makeInitialState('oma-pc')
    s = applyChatEvent(s, { type: 'pending', tool: 't', args: {}, agent_id: 'oma-pc' })
    s = applyChatEvent(s, { type: 'error', error: 'agent unreachable' })
    expect(s.streaming).toBe(false)
    expect(s.pendingGate).toBeNull()
    expect(s.items.some((i) => i.kind === 'error' && i.error === 'agent unreachable')).toBe(true)
  })
})

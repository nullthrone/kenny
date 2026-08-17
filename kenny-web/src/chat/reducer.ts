/**
 * Pure transcript reducer. Kept free of `fetch`/timers/React so the rules
 * around the confirm gate — the security-critical part of this feature —
 * can be asserted with plain data in a test, not through a rendered
 * component and a mocked stream.
 *
 * The one rule this file must never grow: no classification of which tools
 * are "safe". It only ever reacts to which `ChatEvent.type` the server sent
 * (`pending` vs `tool_result`) — see the module docstring in `api/types.ts`.
 */
import type { ChatEvent } from '../api/types'
import type { ChatSessionState, TranscriptItem } from './types'

function nextId(state: ChatSessionState): [string, ChatSessionState] {
  const id = `item-${state.seq}`
  return [id, { ...state, seq: state.seq + 1 }]
}

function pushItem(state: ChatSessionState, item: TranscriptItem): ChatSessionState {
  return { ...state, items: [...state.items, item] }
}

/** A human sent a message. Live turns draw their own bubble immediately —
 * the server never emits `user_text` for a message it is currently handling,
 * only during history replay (`api/types.ts`'s `ChatEvent` doc comment). */
export function startUserTurn(state: ChatSessionState, message: string): ChatSessionState {
  const [id, s1] = nextId(state)
  return {
    ...pushItem(s1, { kind: 'user', id, text: message }),
    streaming: true,
    openAssistantId: null,
  }
}

/**
 * Folds one `ChatEvent` into the session state. Used identically for a live
 * stream and for replaying a stored `transcript` from `/api/chat/history/{id}`
 * — replay is just calling this in a loop, which is exactly why event
 * shapes are shared between the two.
 */
export function applyChatEvent(state: ChatSessionState, event: ChatEvent): ChatSessionState {
  switch (event.type) {
    case 'user_text': {
      const [id, s1] = nextId(state)
      return { ...pushItem(s1, { kind: 'user', id, text: event.text }), openAssistantId: null }
    }

    case 'text_delta': {
      // Accumulate raw text into the open assistant item and re-render the
      // whole buffer on every delta — never append incrementally. A delta
      // can split a markdown token mid-way (non-negotiable #5); storing the
      // full buffer and re-parsing it whole on each render is what makes
      // that safe, and is the natural behaviour of a React string prop.
      if (state.openAssistantId) {
        return {
          ...state,
          items: state.items.map((it) =>
            it.kind === 'assistant' && it.id === state.openAssistantId
              ? { ...it, text: it.text + event.text }
              : it,
          ),
        }
      }
      const [id, s1] = nextId(state)
      return { ...pushItem(s1, { kind: 'assistant', id, text: event.text }), openAssistantId: id }
    }

    case 'tool_result': {
      // A gate this session is currently resolving closes here: update the
      // matching `gate` item in place instead of appending a second row.
      if (state.resolvingGateItemId) {
        const gateId = state.resolvingGateItemId
        return {
          ...state,
          resolvingGateItemId: null,
          pendingGate: null,
          deciding: false,
          openAssistantId: null,
          items: state.items.map((it) =>
            it.kind === 'gate' && it.id === gateId ? { ...it, resolution: 'approved', ok: event.ok } : it,
          ),
        }
      }
      const [id, s1] = nextId(state)
      return {
        ...pushItem(s1, {
          kind: 'auto_run',
          id,
          tool: event.tool,
          ok: event.ok,
          imageB64: event.image_b64,
          format: event.format,
        }),
        openAssistantId: null,
      }
    }

    case 'pending': {
      // THE GATE. The call has not run. No client-side judgment about the
      // tool happens here — the event type itself is the entire signal.
      const [id, s1] = nextId(state)
      const item: TranscriptItem = {
        kind: 'gate',
        id,
        tool: event.tool,
        args: event.args,
        agentId: event.agent_id,
        toolClass: event.tool_class,
        resolution: 'pending',
      }
      return {
        ...pushItem(s1, item),
        pendingGate: { itemId: id, tool: event.tool, args: event.args, agentId: event.agent_id, toolClass: event.tool_class },
        openAssistantId: null,
        // The server closes the initial stream here — nothing is in flight
        // again until a separate POST to /api/chat/confirm/stream. The
        // composer stays locked regardless, because `pendingGate` is set.
        streaming: false,
      }
    }

    case 'denied': {
      if (state.resolvingGateItemId) {
        const gateId = state.resolvingGateItemId
        return {
          ...state,
          resolvingGateItemId: null,
          pendingGate: null,
          deciding: false,
          openAssistantId: null,
          items: state.items.map((it) => (it.kind === 'gate' && it.id === gateId ? { ...it, resolution: 'denied' } : it)),
        }
      }
      // Defensive fallback — the contract says `denied` only follows a
      // confirm(false), but if it ever arrives unmatched, still surface it
      // rather than silently dropping a denial.
      const [id, s1] = nextId(state)
      return { ...pushItem(s1, { kind: 'denied', id, tool: event.tool, message: event.message }), openAssistantId: null }
    }

    case 'done': {
      return {
        ...state,
        streaming: false,
        openAssistantId: null,
        sessionId: event.session_id ?? state.sessionId,
      }
    }

    case 'error': {
      const [id, s1] = nextId(state)
      return {
        ...pushItem(s1, { kind: 'error', id, error: event.error }),
        streaming: false,
        pendingGate: null,
        deciding: false,
        resolvingGateItemId: null,
        openAssistantId: null,
        sessionId: event.session_id ?? state.sessionId,
      }
    }

    /**
     * Emitted only by the recommendation stream, which a host section modal
     * consumes directly — it never reaches a chat transcript. The event union is
     * shared across all four streams, so it is named here rather than left to the
     * exhaustiveness check, which would otherwise fail the build.
     */
    case 'remediation':
      return state

    default: {
      const _exhaustive: never = event
      return _exhaustive
    }
  }
}

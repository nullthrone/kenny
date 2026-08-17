/**
 * A single module-level chat session, deliberately not React state.
 *
 * Shell (outside our ownership, see Shell.tsx) mounts the drawer's content
 * only while `chatOpen` is true and fully unmounts it on close — there is
 * no persistent provider above it we're allowed to add. Keeping the session
 * in a plain singleton, subscribed to via `useSyncExternalStore`, means the
 * transcript — and critically, an unresolved confirm gate — survives the
 * drawer being closed and reopened (⌘K again) within the same page load.
 * That matters: if an operator closes the drawer chrome while a gate is
 * pending (Shell's header ✕ and backdrop are outside this module's control
 * and are not gate-aware), the gate must still be sitting there, still
 * locked, the moment the drawer reopens — never silently discarded.
 *
 * Reloading the page is different and NOT covered here: that's an accepted
 * loss of in-flight UI state (non-negotiable #6) because there is nothing
 * server-durable to rehydrate it from for the fleet-chat gate.
 *
 * Only one conversation is ever active, matching the server's own model
 * (one `session_id` at a time) and the old dashboard's documented invariant
 * that "at most one gate is ever open" (notes/api-contract-actual.md §6).
 */
import { api } from '../api/client'
import { streamChatEvents } from '../api/sse'
import type { ChatStreamRequest } from '../api/types'
import { applyChatEvent, startUserTurn } from './reducer'
import type {
  ChatConfirmRequest,
  ChatHistoryDetailResponse,
  ChatHistoryListResponse,
  ChatSessionState,
  ConversationSummary,
} from './types'
import { makeInitialState } from './types'

type Listener = () => void

class ChatStore {
  private state: ChatSessionState = makeInitialState('')
  private listeners = new Set<Listener>()
  private controller: AbortController | null = null

  getState = (): ChatSessionState => this.state

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  private set(next: ChatSessionState): void {
    this.state = next
    this.listeners.forEach((l) => l())
  }

  private update(fn: (s: ChatSessionState) => ChatSessionState): void {
    this.set(fn(this.state))
  }

  /**
   * Called every time the drawer mounts (each ⌘K/header-button open, since
   * Shell fully unmounts it on close). If the session opened for a
   * different host than this one and nothing security-relevant is in
   * flight, start clean so the scope chip is never showing a host this
   * conversation didn't actually run against. A pending gate or an active
   * turn always wins — never reset out from under those.
   */
  // Every public method below is an arrow-function class field, not a
  // prototype method — deliberately, so `chatStore.sendMessage` etc. are
  // stable references usable directly as hook return values. `useChatSession`
  // hands these straight to components; `HistoryPanel` depends on
  // `listHistory` inside a `useEffect`, and a fresh function identity on
  // every render would re-fire that effect on every unrelated store update.

  openForScope = (agentId: string): void => {
    const s = this.state
    if (s.agentId !== agentId && !s.pendingGate && !s.streaming) {
      this.set(makeInitialState(agentId))
    }
  }

  sendMessage = async (message: string): Promise<void> => {
    const s = this.state
    if (s.streaming || s.pendingGate) return // never overlap turns
    this.update((st) => startUserTurn(st, message))

    const controller = new AbortController()
    this.controller = controller
    // agent_id is ALWAYS sent, even as ''. Omitting the key would leave the
    // server-side session pointed at whatever host was last selected, and
    // the drawer's scope chip would then be lying about what the model can
    // see (non-negotiable #1).
    const body: ChatStreamRequest = {
      session_id: this.state.sessionId,
      message,
      agent_id: s.agentId,
      scope: s.agentId ? 'host' : 'fleet',
    }
    await this.runStream('/api/chat/stream', body, controller)
  }

  resolveGate = async (approve: boolean): Promise<void> => {
    const gate = this.state.pendingGate
    if (!gate || this.state.deciding) return
    // pendingGate stays set — the modal stays open with its buttons disabled
    // (`deciding`) through the whole round-trip. It's cleared only once the
    // reducer sees the matching tool_result/denied land (reducer.ts).
    this.update((st) => ({ ...st, deciding: true, resolvingGateItemId: gate.itemId, streaming: true }))

    const controller = new AbortController()
    this.controller = controller
    const body: ChatConfirmRequest = { session_id: this.state.sessionId, approve }
    await this.runStream('/api/chat/confirm/stream', body, controller)
  }

  private runStream = async (url: string, body: ChatStreamRequest | ChatConfirmRequest, controller: AbortController): Promise<void> => {
    try {
      for await (const event of streamChatEvents(url, body, { signal: controller.signal })) {
        this.update((st) => applyChatEvent(st, event))
      }
    } catch (err) {
      if (controller.signal.aborted) {
        // Deliberate Stop — not a failure, don't surface an error bubble.
      } else {
        const message = err instanceof Error ? err.message : String(err)
        this.update((st) => applyChatEvent(st, { type: 'error', error: message }))
      }
    } finally {
      if (this.controller === controller) this.controller = null
      // Belt-and-suspenders: guarantee the composer never gets stuck locked
      // if the stream ends without an explicit `done`/`error` event.
      this.update((st) => (st.streaming ? { ...st, streaming: false } : st))
    }
  }

  stop = (): void => {
    this.controller?.abort()
  }

  /** Discards the current conversation and starts a new one scoped to `agentId`. */
  reset = (agentId: string): void => {
    this.controller?.abort()
    this.set(makeInitialState(agentId))
  }

  loadConversation = async (id: string): Promise<void> => {
    this.controller?.abort()
    const detail = await api.get<ChatHistoryDetailResponse>(`/api/chat/history/${encodeURIComponent(id)}`)
    let replayed: ChatSessionState = { ...makeInitialState(detail.agent_id), sessionId: detail.id }
    for (const event of detail.transcript) {
      replayed = applyChatEvent(replayed, event)
    }
    // A replayed turn is never "in flight" — only a genuinely pending gate
    // (replayed from the transcript itself) should still lock the composer.
    replayed = { ...replayed, streaming: false }
    this.set(replayed)
  }

  listHistory = async (): Promise<ConversationSummary[]> => {
    const res = await api.get<ChatHistoryListResponse>('/api/chat/history')
    return res.conversations
  }

  deleteConversation = async (id: string): Promise<void> => {
    await api.delete(`/api/chat/history/${encodeURIComponent(id)}`)
    if (this.state.sessionId === id) {
      this.set(makeInitialState(this.state.agentId))
    }
  }
}

export const chatStore = new ChatStore()

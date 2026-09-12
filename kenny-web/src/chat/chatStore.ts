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
import { announceTicketTurn } from './ticketTurn'
import type {
  ChatConfirmRequest,
  ChatHistoryDetailResponse,
  ChatHistoryListResponse,
  ChatSessionState,
  ConversationSummary,
  TicketChatRequest,
  TicketChatTarget,
  TicketDecisionRequest,
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
    if ((s.agentId !== agentId || s.ticket !== null) && !s.pendingGate && !s.streaming) {
      this.set(makeInitialState(agentId))
    }
  }

  /**
   * Point the conversation at a ticket, or refresh what it knows about the
   * one it is already on.
   *
   * Called by the ticket page on every change to the ticket it is showing, so
   * "is this parked on an approval right now" stays true without the drawer
   * fetching anything. Switching to a *different* ticket starts clean, for
   * the same reason `openForScope` does: the chip must never name a ticket
   * this conversation did not run against. Rebinding the same ticket only
   * updates the details — a live transcript is not thrown away because the
   * ticket's state moved underneath it.
   */
  openForTicket = (ticket: TicketChatTarget): void => {
    const s = this.state
    if (s.ticket?.id === ticket.id) {
      // Rebinding the same ticket only refreshes what the drawer knows —
      // including whether a gate is open, which is why a decision made from
      // Discord or by another operator quietly takes the card away here, and
      // why one made here stops offering itself the moment the server agrees.
      this.update((st) => ({ ...st, agentId: ticket.agentId, ticket }))
      return
    }
    if (s.pendingGate || s.streaming) return
    this.set(makeInitialState(ticket.agentId, ticket))
  }

  /**
   * `mirrorToDiscord` applies to a ticket turn only, and is a per-send choice
   * rather than session state: the ticket's own trail is the record either
   * way, and whether this particular answer should also land in the family's
   * thread is decided per answer.
   */
  sendMessage = async (message: string, mirrorToDiscord = false): Promise<void> => {
    const s = this.state
    // Never overlap turns — and a ticket's durable gate counts, whoever raised
    // it: kenny is waiting on that answer before it can do anything else.
    if (s.streaming || s.pendingGate || s.ticket?.gate) return
    this.update((st) => startUserTurn(st, message))

    const controller = new AbortController()
    this.controller = controller
    if (s.ticket) {
      // The ticket's own surface: a different endpoint, a different gate
      // (`TicketPolicy` — read-only runs, a standard change runs and says so,
      // a consequential one holds for an operator), and a host that is the
      // ticket's frozen `agent_id` and is never sent from here at all
      // (ADR-0050). The event vocabulary is deliberately the same one, which
      // is why the reducer below does not know the difference.
      await this.runStream(
        `/api/tickets/${encodeURIComponent(s.ticket.id)}/chat/stream`,
        { message, mirror_to_discord: mirrorToDiscord },
        controller,
      )
      return
    }
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

  /**
   * Decide the open gate — the copilot's transient one, or the ticket's durable
   * one — and stream what the decision releases straight back into this
   * transcript.
   *
   * A ticket's gate is still durable, still the ticket's, and may still be
   * decided by a different operator or from Discord (ADR-0046, ADR-0050);
   * what changed is where *this* operator answers it. Deciding it here is not
   * deciding without evidence — the frozen call and its arguments are on the
   * card being clicked — and it is the only way the conversation that raised
   * the gate can carry on without a reload: the server ends the turn's stream
   * at `pending`, so the continuation belongs to whichever request resolves it.
   */
  resolveGate = async (approve: boolean): Promise<void> => {
    const s = this.state
    if (s.deciding) return
    // pendingGate/gate stays set — the card stays up with its buttons disabled
    // (`deciding`) through the whole round-trip. It is cleared only once the
    // reducer sees the matching tool_result/denied land (reducer.ts), or, for a
    // ticket, once the resumed turn ends.
    const ticketGate = s.ticket?.gate ?? null
    if (ticketGate) {
      this.update((st) => ({
        ...st,
        deciding: true,
        // Only if this session is the one that raised it: a gate opened by an
        // investigation nobody started has no row here to resolve in place.
        resolvingGateItemId: st.pendingGate?.itemId ?? null,
        streaming: true,
      }))
      const controller = new AbortController()
      this.controller = controller
      const body: TicketDecisionRequest = { approve }
      await this.runStream(
        `/api/tickets/${encodeURIComponent(s.ticket!.id)}/approvals/${encodeURIComponent(ticketGate.id)}/decide/stream`,
        body,
        controller,
      )
      // The ticket page re-reads the durable state and re-binds (`openForTicket`);
      // this only stops the card being offered a second time in the meantime.
      this.update((st) =>
        st.ticket && st.ticket.gate?.id === ticketGate.id
          ? { ...st, deciding: false, ticket: { ...st.ticket, gate: null } }
          : { ...st, deciding: false },
      )
      return
    }

    const gate = s.pendingGate
    if (!gate) return
    this.update((st) => ({ ...st, deciding: true, resolvingGateItemId: gate.itemId, streaming: true }))

    const controller = new AbortController()
    this.controller = controller
    const body: ChatConfirmRequest = { session_id: this.state.sessionId, approve }
    await this.runStream('/api/chat/confirm/stream', body, controller)
  }

  /**
   * Open the ticket a draft card is showing, with whatever the operator edited
   * into it.
   *
   * The POST is the inbox's own — the only route that opens a ticket — and the
   * values are the form's, never the model's: kenny proposed, the operator is
   * filing. `chat_session_id` lets the server note what this conversation had
   * already checked; it derives that itself, so nothing here can claim a check
   * that did not happen.
   */
  createFromDraft = async (
    itemId: string,
    draft: { title: string; summary: string; agentId: string | null; startImmediately: boolean },
  ): Promise<void> => {
    const body = {
      title: draft.title.trim(),
      summary: draft.summary.trim(),
      agent_id: draft.agentId,
      origin: 'copilot',
      start_immediately: draft.startImmediately,
      chat_session_id: this.state.sessionId,
    }
    const ticket = await api.post<{ id: string; number: number }>('/api/tickets', body)
    this.update((st) => ({
      ...st,
      items: st.items.map((it) =>
        it.kind === 'draft' && it.id === itemId
          ? {
              ...it,
              title: body.title,
              summary: body.summary,
              agentId: draft.agentId ?? '',
              resolution: 'created' as const,
              ticketId: ticket.id,
              ticketNumber: ticket.number,
            }
          : it,
      ),
    }))
  }

  /** Put a draft away unfiled. The row stays: the offer was made and declined. */
  dismissDraft = (itemId: string): void => {
    this.update((st) => ({
      ...st,
      items: st.items.map((it) =>
        it.kind === 'draft' && it.id === itemId
          ? { ...it, resolution: 'dismissed' as const }
          : it,
      ),
    }))
  }

  private runStream = async (
    url: string,
    body: ChatStreamRequest | ChatConfirmRequest | TicketChatRequest | TicketDecisionRequest,
    controller: AbortController,
  ): Promise<void> => {
    const ticketId = this.state.ticket?.id ?? null
    try {
      for await (const event of streamChatEvents(url, body, { signal: controller.signal })) {
        this.update((st) => applyChatEvent(st, event))
        // A ticket turn writes to the ticket's trail as it goes: its message,
        // kenny's reply, every call. Tell the page at the two moments its own
        // view is now behind — a gate opening, and the turn ending.
        if (ticketId && (event.type === 'pending' || event.type === 'done')) {
          announceTicketTurn(ticketId)
        }
      }
    } catch (err) {
      if (controller.signal.aborted) {
        // Deliberate Stop — not a failure, don't surface an error bubble.
      } else {
        const message = err instanceof Error ? err.message : String(err)
        this.update((st) => applyChatEvent(st, { type: 'error', error: message }))
        if (ticketId) announceTicketTurn(ticketId)
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

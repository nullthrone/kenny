/**
 * Local chat state shapes. `ChatEvent`/`ChatStreamRequest` come from the
 * frozen `src/api/types.ts` and are re-exported nowhere else — import them
 * directly from there. Everything in this file is ours to define because
 * the frozen contract only states the wire shapes, not how the drawer
 * keeps score between them.
 */
import type { ChatEvent } from '../api/types'

/** `POST /api/chat/confirm/stream` body. Not in the frozen contract (chat/stream's
 * sibling), documented only in notes/api-contract-actual.md §2 item 2. */
export interface ChatConfirmRequest {
  session_id: string | null
  approve: boolean
}

/**
 * `POST /api/tickets/{id}/approvals/{aid}/decide/stream` body.
 *
 * A ticket's gate is durable and belongs to the ticket, so the decision is
 * addressed by approval id rather than by session — and the response is the
 * turn the decision releases, streamed in the same event vocabulary a chat turn
 * uses, so the conversation carries on where it stopped.
 */
export interface TicketDecisionRequest {
  approve: boolean
}

/**
 * The gate a ticket is holding, as the ticket page read it from
 * `/api/approvals?ticket_id=…`.
 *
 * Present whoever raised it: a turn driven from this drawer, a Discord message,
 * or an unprompted investigation nobody started. That is the point — the panel
 * is where a decision is made, so it has to be able to show one it has no
 * transcript for.
 */
export interface TicketGate {
  id: string
  tool: string
  /** Frozen at hold time. Rendered verbatim by `GateCard`; never touched here. */
  args: Record<string, unknown>
  agentId?: string | null
  toolClass?: string
}

/**
 * `POST /api/tickets/{id}/chat/stream` body. No `agent_id` and no `scope`:
 * the ticket's target is frozen server-side and a caller cannot move it
 * (ADR-0038), so there is nothing here to get wrong.
 */
export interface TicketChatRequest {
  message: string
  mirror_to_discord: boolean
}

/** One row of `GET /api/chat/history`. */
export interface ConversationSummary {
  id: string
  title: string
  updated_at: string
  agent_id: string
}

export interface ChatHistoryListResponse {
  conversations: ConversationSummary[]
}

/** `GET /api/chat/history/{id}` — `transcript` is replayed through the same reducer a live turn uses. */
export interface ChatHistoryDetailResponse {
  id: string
  agent_id: string
  transcript: ChatEvent[]
}

/**
 * One row in the transcript the drawer renders. This is a client-side
 * projection built by folding `ChatEvent`s through the reducer — it is not
 * itself part of the wire contract.
 */
export type TranscriptItem =
  | { kind: 'user'; id: string; text: string }
  | { kind: 'assistant'; id: string; text: string }
  /**
   * Kenny's reasoning, kept apart from its answer and rendered folded. It is a
   * live view only: the server stores none of it, so a replayed conversation
   * and a ticket's timeline never show one of these.
   */
  | { kind: 'thinking'; id: string; text: string }
  | { kind: 'auto_run'; id: string; tool: string; ok: boolean; imageB64?: string; format?: string }
  | { kind: 'denied'; id: string; tool: string; message?: string }
  | {
      kind: 'gate'
      id: string
      tool: string
      args: Record<string, unknown>
      agentId: string
      toolClass?: string
      /**
       * 'pending' while the decision is outstanding — that state is the
       * gate. 'approved'/'denied' once a confirm round-trip resolved it;
       * the item stays in the transcript as a record, it does not disappear.
       */
      resolution: 'pending' | 'approved' | 'denied'
      /** Set once resolution is 'approved' and the tool_result for it has arrived. */
      ok?: boolean
    }
  /**
   * A ticket kenny proposed, as an editable form. Shaped after `gate`: a card
   * that carries a decision and stays in the transcript once it is made, so a
   * reader can see the ticket came out of this conversation.
   *
   * `resolution` is the operator's, never the model's — nothing about this item
   * changes until they submit or dismiss the form, because until then the
   * ticket does not exist.
   */
  | {
      kind: 'draft'
      id: string
      title: string
      summary: string
      agentId: string
      resolution: 'pending' | 'created' | 'dismissed'
      /** Set once `resolution` is 'created': what the form actually opened. */
      ticketId?: string
      ticketNumber?: number
    }
  | { kind: 'error'; id: string; error: string }

export interface PendingGate {
  /** The transcript item id of the matching `gate` row, so resolution can update it in place. */
  itemId: string
  tool: string
  args: Record<string, unknown>
  agentId: string
  toolClass?: string
}

/**
 * The ticket a conversation is bound to, when it is bound to one.
 *
 * Seeded by the ticket page (`InboxTicket`), which is the only place that
 * knows a ticket's number, its frozen host, whether it has a Discord thread
 * and whether it is currently parked on an approval. The drawer reads it and
 * never fetches it, so the drawer stays renderable without a query client.
 *
 * Its presence changes three things and nothing else: which endpoint a turn
 * posts to, which endpoint a decision posts to (the ticket's durable gate, by
 * approval id, rather than the copilot's session-scoped confirm), and that the
 * conversation has no `/api/chat/history` of its own — the ticket's timeline is
 * its history.
 */
export interface TicketChatTarget {
  id: string
  number: number
  /** The ticket's frozen `agent_id`. Nothing in the conversation can move it. */
  agentId: string
  discordThread: boolean
  assistantAvailable: boolean
  /** The open gate, or null. Non-null locks the composer: kenny is waiting on
   * this answer and nothing else can be asked of it until it has one. */
  gate: TicketGate | null
}

export interface ChatSessionState {
  /** The scope this conversation is committed to. Never changes mid-conversation
   * (ADR-0045 in the frozen contract's comment: the tier is a tool property,
   * the gate is a calling-surface property, but the SCOPE a session was opened
   * with must stay put so the scope chip never lies about what the model saw). */
  agentId: string
  sessionId: string | null
  items: TranscriptItem[]
  /**
   * Non-null = the confirm gate is open OR a decision on it is in flight
   * (`deciding`). The composer MUST be locked whenever this is set — it is
   * cleared only once the confirm/stream round-trip actually resolves the
   * call, not the moment CONFIRM/CANCEL is clicked, so the gate modal stays
   * up (buttons disabled via `deciding`) through the whole round-trip
   * instead of vanishing before anything has actually happened.
   */
  pendingGate: PendingGate | null
  /** True from the moment CONFIRM/CANCEL is clicked until its result lands. */
  deciding: boolean
  /** Set when a decision has been posted and we're waiting on its result to land,
   * so the matching `gate` transcript item is updated in place rather than a
   * second item being appended. */
  resolvingGateItemId: string | null
  /** A turn (initial send or confirm) is actively streaming from the server. */
  streaming: boolean
  /** id of the assistant transcript item currently accumulating `text_delta`s, if any. */
  openAssistantId: string | null
  /** id of the thinking item currently accumulating `thinking_delta`s, if any.
   * Non-null means kenny is still reasoning — which is what the folded block's
   * own label says, so it is never inferred from `streaming` alone. */
  openThinkingId: string | null
  /** Monotonic counter backing transcript item ids — keeps id generation pure/deterministic. */
  seq: number
  /** Non-null when this conversation is a ticket's own (ADR-0050). */
  ticket: TicketChatTarget | null
}

export function makeInitialState(
  agentId: string,
  ticket: TicketChatTarget | null = null,
): ChatSessionState {
  return {
    agentId,
    ticket,
    sessionId: null,
    items: [],
    pendingGate: null,
    deciding: false,
    resolvingGateItemId: null,
    streaming: false,
    openAssistantId: null,
    openThinkingId: null,
    seq: 0,
  }
}

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
  | { kind: 'error'; id: string; error: string }

export interface PendingGate {
  /** The transcript item id of the matching `gate` row, so resolution can update it in place. */
  itemId: string
  tool: string
  args: Record<string, unknown>
  agentId: string
  toolClass?: string
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
  /** Monotonic counter backing transcript item ids — keeps id generation pure/deterministic. */
  seq: number
}

export function makeInitialState(agentId: string): ChatSessionState {
  return {
    agentId,
    sessionId: null,
    items: [],
    pendingGate: null,
    deciding: false,
    resolvingGateItemId: null,
    streaming: false,
    openAssistantId: null,
    seq: 0,
  }
}

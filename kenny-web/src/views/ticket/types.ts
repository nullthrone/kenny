/**
 * Local shapes for the ticket-detail surface.
 *
 * `src/api/types.ts` is frozen and only covers Inbox's merged-queue shapes
 * (`InboxItem`/`InboxGate`/`TicketSummary`) — it deliberately does not
 * define a full `Ticket`/`TicketEvent`/vocabulary shape, since ticket
 * detail wasn't in scope when that contract was authored. These are
 * transcribed from the server's actual response shapes
 * (`kenny_server/ticketstore.py`'s dataclasses' `as_dict()`,
 * `kenny_server/webui/tickets.py`'s `_affordances`/`api_tickets_vocabulary`)
 * rather than invented — field names are the server's, verbatim, same rule
 * as the frozen file.
 */

/** `GET /api/tickets/vocabulary` — server-authoritative lifecycle vocabulary. */
export interface TicketVocabulary {
  states: string[]
  blocked_reasons: string[]
  priorities: string[]
  categories: string[]
}

/**
 * A ticket, as returned by `GET/PATCH /api/tickets/{id}` and the list/create
 * routes. `allowed_transitions`/`can_unblock` are computed server-side per the
 * calling principal (`_affordances`) — THE ONLY source of which lifecycle
 * buttons may render. Never derive these from `state` client-side; `state`
 * only decides which of the licensed moves is the forward one, and its label.
 *
 * `allowed_blocks` is always empty: a block is set where the wait it names
 * begins, so no principal may set one over HTTP. The field stays on the
 * payload because `_affordances` still computes it from `can_block`, which is
 * what keeps that guarantee checkable rather than assumed.
 */
export interface Ticket {
  id: string
  number: number
  title: string
  state: string
  origin: string
  priority: string
  category: string | null
  requester_user_id: number | null
  agent_id: string | null
  role_snapshot: string | null
  profile_snapshot: string | null
  summary: string
  resolution: string | null
  created_at: string
  updated_at: string
  closed_at: string | null
  /** `''` when nothing is blocking — mirrors `InboxItem.waits_on`. */
  blocked_on: string
  blocked_since: string | null
  blocked_ref: string
  /**
   * Retired: nothing writes this any more and nothing here reads it. Still on
   * the payload because rows created before it was retired carry a value.
   */
  assignee_user_id: number | null
  /**
   * `'triage'` when an unprompted investigation put the ticket in its current
   * state, `''` otherwise. Rewritten on every transition, so it describes the
   * state now — a reopened ticket no longer claims kenny resolved it.
   */
  resolved_by: string
  allowed_transitions: string[]
  allowed_blocks: string[]
  can_unblock: boolean
  /** Whether the ticket's own Ask-kenny composer can be driven at all. */
  assistant_available: boolean
  /** Whether there's a Discord thread to optionally mirror a chat turn into. Present on the single-ticket GET only. */
  discord_thread?: boolean
}

/** One row of a ticket's audit trail, `GET /api/tickets/{id}/events`. */
export interface TicketEvent {
  id: number
  ticket_id: string
  at: string
  kind: string
  actor: string
  tool: string | null
  tool_class: string | null
  ok: boolean | null
  from_state: string | null
  to_state: string | null
  summary: string
  fields: Record<string, unknown> | null
}

/**
 * One row of the ticket's *presented* timeline, `GET /api/tickets/{id}/timeline`.
 *
 * The projection twin of `TicketEvent`, composed server-side
 * (`kenny_server/ticket_timeline.py`). It is deliberately not derived here:
 * how a trail row reads — condensed, dropped, or turned into a sentence — is
 * one decision, and the server is where the ticket's other surfaces can reach
 * it too. The client's job is to render `text` according to `body`.
 */
export interface TimelineEntry {
  at: string
  actor: string
  /** `message` | `finding` | `activity` | `action` | `lifecycle` | `problem`. */
  kind: string
  text: string
  /** `markdown` (kenny's own prose) | `verbatim` (a person's typing) | `status` (composed). */
  body: string
  /**
   * The trail rows this entry stands for. Renderable proof that the
   * presentation invented nothing — and the way back into the audit tab.
   */
  source_event_ids: number[]
  /** Only a `finding` carries these: the verdict payload, unchanged. */
  fields: Record<string, unknown>
}

/** A durable gate row — `GET /api/approvals`, and embedded in `POST /api/approvals/{id}`'s response. */
export interface TicketApproval {
  id: string
  ticket_id: string
  tool_use_id: string
  tool: string
  tool_class: string
  args: Record<string, unknown>
  agent_id: string | null
  kind: string
  status: string
  requested_at: string
  expires_at: string | null
  decided_at: string | null
  decided_by: number | null
  decided_via: string | null
}

/**
 * `POST /api/approvals/{id}` response. `resumed === false` means the
 * decision was recorded but kenny could not continue the ticket
 * automatically — must be reported distinctly from plain success.
 */
export interface ApprovalDecideResponse extends TicketApproval {
  resumed: boolean
  resume_status: string
}

/** `GET /api/users/directory` — operator+ only. Resolves actor ids to names. */
export interface DirectoryUser {
  id: number
  username: string
  role: string
}

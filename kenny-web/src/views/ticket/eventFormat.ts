import type { DirectoryUser, TicketEvent } from './types'

/** `TicketEvent.at` (server ISO timestamp) → the timeline's `19:41`-style clock string. */
export function formatEventTime(at: string): string {
  const date = new Date(at)
  if (Number.isNaN(date.getTime())) return at
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

/**
 * `TicketService`'s actor string, verbatim: `"operator:<uid>"`,
 * `"user:<uid>"`, bare `"operator"`/`"user"` (shared-token/no user row),
 * `"assistant"` (kenny itself), or `"system"`.
 */
export function actorLabel(actor: string, directory: DirectoryUser[] | undefined): string {
  if (actor === 'assistant') return 'KENNY'
  if (actor === 'triage') return 'KENNY · UNPROMPTED'
  if (actor === 'system') return 'SYSTEM'
  const [role, idPart] = actor.split(':')
  const id = idPart !== undefined ? Number(idPart) : NaN
  const user = Number.isFinite(id) ? directory?.find((u) => u.id === id) : undefined
  if (user) return user.username.toUpperCase()
  if (Number.isFinite(id)) return `${role.toUpperCase()} #${id}`
  return role.toUpperCase()
}

/**
 * Whether an actor is kenny itself. `triage` counts: it is the same assistant
 * writing the same way — the label carries the one thing that differs, that
 * nobody asked it to look.
 */
export function isAssistant(actor: string): boolean {
  return actor === 'assistant' || actor === 'triage'
}

export function actorColor(actor: string): string {
  if (isAssistant(actor)) return 'var(--brass-600)'
  if (actor === 'system') return 'var(--text-faint)'
  return 'var(--text-muted)'
}

export function actorDot(actor: string): string {
  if (isAssistant(actor)) return 'var(--brass-500)'
  if (actor === 'system') return 'var(--ink-200)'
  return 'var(--ink-300)'
}

/** The five verdicts `ticket_triage_verdict` may report (`toolloop.TRIAGE_VERDICTS`). */
export const TRIAGE_VERDICTS = [
  'phantom',
  'benign_known',
  'resolved_itself',
  'actionable',
  'inconclusive',
] as const

export type TriageVerdict = (typeof TRIAGE_VERDICTS)[number]

/**
 * How a verdict reads at a glance. Three colours, not five: the only
 * distinction the reader acts on is "nothing to do" / "your turn" / "nobody
 * knows yet". Which of the three benign shapes it was matters when you read
 * the finding, not when you scan the timeline.
 */
export function verdictTone(verdict: string): 'settled' | 'attention' | 'unclear' {
  if (verdict === 'actionable') return 'attention'
  // Unknown falls to `unclear`, not `settled`. The five verdicts live on the
  // server (`toolloop.TRIAGE_VERDICTS`), so a build of this UI can be older
  // than the set — and a verdict word it has never heard of must not be
  // painted as an all-clear. "I don't recognise this" reads as unclear, which
  // is what it is.
  if (verdict === 'phantom' || verdict === 'benign_known' || verdict === 'resolved_itself') {
    return 'settled'
  }
  return 'unclear'
}

export function verdictLabel(verdict: string): string {
  return verdict.replace(/_/g, ' ').toUpperCase()
}

/** A suppression an investigation proposed — a suggestion, never a rule. */
export interface SuppressionSuggestion {
  source: string
  event_id: number
}

/** The parts of a triage verdict the timeline renders. */
export interface TriageFinding {
  verdict: string
  finding: string
  evidence: string
  /** Present only when the server declined to act on the verdict, and says why. */
  notResolvedBecause: string | null
  suggestion: SuppressionSuggestion | null
}

function asSuggestion(value: unknown): SuppressionSuggestion | null {
  const raw = asRecord(value)
  if (!raw) return null
  const source = typeof raw.source === 'string' ? raw.source : ''
  const eventId = typeof raw.event_id === 'number' ? raw.event_id : null
  return source && eventId !== null ? { source, event_id: eventId } : null
}

/**
 * A triage verdict, or null for every other note.
 *
 * An investigation writes two kinds of note: the "looking into this" line it
 * opens with, and the verdict it ends with. Only the second carries a
 * `verdict` field, and only the second is worth more than one line — so the
 * field, not the actor, is what decides.
 */
export function triageFinding(event: TicketEvent): TriageFinding | null {
  if (event.kind !== 'note' || event.actor !== 'triage') return null
  const fields = asRecord(event.fields)
  const verdict = typeof fields?.verdict === 'string' ? fields.verdict : ''
  if (!verdict) return null
  const why = typeof fields?.not_resolved_because === 'string' ? fields.not_resolved_because : ''
  return {
    verdict,
    finding: typeof fields?.finding === 'string' ? fields.finding : '',
    evidence: typeof fields?.evidence === 'string' ? fields.evidence : '',
    notResolvedBecause: why || null,
    suggestion: asSuggestion(fields?.suppression_suggestion),
  }
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : undefined
}

/**
 * How `FormattedEvent.text` must be rendered.
 *
 * `markdown` is kenny's own prose, and only kenny's: the conversational system
 * prompts ask for markdown and the model writes it. `verbatim` is text a person
 * typed — their line breaks are kept, but their `*` and `_` are theirs, never
 * markup. `status` is a line this module composed from the event's own fields;
 * parsing it could only misread it, since a tool name beginning with `-` is not
 * a bullet.
 */
export type EventBody = 'markdown' | 'verbatim' | 'status'

export interface FormattedEvent {
  who: string
  whoColor: string
  dot: string
  text: string
  body: EventBody
  /** Rendered verbatim in mono, same discipline as a gate's frozen args — never reformatted. */
  mono: string | null
}

function eventBody(event: TicketEvent): EventBody {
  if (event.kind === 'message') return isAssistant(event.actor) ? 'markdown' : 'verbatim'
  // An operator's note is free text they typed; triage's own notes are either a
  // fixed line or a verdict the timeline renders as a finding, not as prose.
  if (event.kind === 'note') return 'verbatim'
  return 'status'
}

/**
 * Renders one `TicketEvent` for the timeline. `kind` is one of the ten the
 * server writes (`state`, `block`, `handoff`, `assign`, `approval`,
 * `consent`, `tool_call`, `message`, `note`, `error` —
 * `kenny_server/tickets.py`'s `EVENT_KINDS` plus the four chokepoint kinds).
 * Text is composed only from fields the server actually sent — never
 * inferred from `kind` alone — falling back to the server's own `summary`
 * wherever a kind carries no more specific field to read.
 */
export function formatEvent(event: TicketEvent, directory: DirectoryUser[] | undefined): FormattedEvent {
  const who = actorLabel(event.actor, directory)
  const fields = asRecord(event.fields)
  const base = {
    who: event.kind === 'message' && fields?.surface === 'discord' ? `${who} · DISCORD` : who,
    whoColor: actorColor(event.actor),
    dot: actorDot(event.actor),
    body: eventBody(event),
  }

  switch (event.kind) {
    case 'state': {
      const text =
        event.summary ||
        (event.from_state ? `moved from ${event.from_state} to ${event.to_state}` : `opened as ${event.to_state}`)
      return { ...base, text, mono: null }
    }
    case 'block': {
      const to = fields?.to_blocked_on
      const text =
        event.summary || (to ? `blocked on ${String(to)}` : 'unblocked')
      return { ...base, text, mono: null }
    }
    case 'handoff':
    case 'assign':
    case 'note':
      return { ...base, text: event.summary || event.kind, mono: null }
    case 'message': {
      const text = typeof fields?.text === 'string' ? fields.text : event.summary || 'message'
      return { ...base, text, mono: null }
    }
    case 'approval':
    case 'consent': {
      const args = fields?.args
      const mono = args && typeof args === 'object' ? `${event.tool ?? ''} ${JSON.stringify(args)}` : null
      return { ...base, text: event.summary, mono }
    }
    case 'tool_call': {
      const args = fields?.args
      const okLabel = event.ok === false ? 'failed' : event.ok === true ? 'ok' : ''
      const mono =
        args && typeof args === 'object'
          ? `${event.tool ?? ''} ${JSON.stringify(args)}${okLabel ? ` · ${okLabel}` : ''}`
          : null
      return { ...base, text: event.summary, mono }
    }
    case 'error': {
      const err = fields?.error
      const mono = err ? JSON.stringify(err) : null
      return { ...base, text: event.summary || 'error', mono }
    }
    default:
      return { ...base, text: event.summary || event.kind, mono: null }
  }
}

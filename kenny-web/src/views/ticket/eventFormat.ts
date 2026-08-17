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
  if (actor === 'system') return 'SYSTEM'
  const [role, idPart] = actor.split(':')
  const id = idPart !== undefined ? Number(idPart) : NaN
  const user = Number.isFinite(id) ? directory?.find((u) => u.id === id) : undefined
  if (user) return user.username.toUpperCase()
  if (Number.isFinite(id)) return `${role.toUpperCase()} #${id}`
  return role.toUpperCase()
}

export function actorColor(actor: string): string {
  if (actor === 'assistant') return 'var(--brass-600)'
  if (actor === 'system') return 'var(--text-faint)'
  return 'var(--text-muted)'
}

export function actorDot(actor: string): string {
  if (actor === 'assistant') return 'var(--brass-500)'
  if (actor === 'system') return 'var(--ink-200)'
  return 'var(--ink-300)'
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : undefined
}

export interface FormattedEvent {
  who: string
  whoColor: string
  dot: string
  text: string
  /** Rendered verbatim in mono, same discipline as a gate's frozen args — never reformatted. */
  mono: string | null
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

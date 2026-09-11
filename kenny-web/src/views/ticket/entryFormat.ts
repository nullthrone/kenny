import type { TimelineEntry } from './types'

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

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : undefined
}

function asSuggestion(value: unknown): SuppressionSuggestion | null {
  const raw = asRecord(value)
  if (!raw) return null
  const source = typeof raw.source === 'string' ? raw.source : ''
  const eventId = typeof raw.event_id === 'number' ? raw.event_id : null
  return source && eventId !== null ? { source, event_id: eventId } : null
}

/**
 * A triage verdict, or null for every other entry.
 *
 * The `verdict` field decides, not the entry's kind or its actor — the server
 * only fills `fields` for a finding, and a build of this UI that has never
 * heard of some later entry kind must still not mistake it for one.
 */
export function findingOf(entry: TimelineEntry): TriageFinding | null {
  const fields = asRecord(entry.fields)
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

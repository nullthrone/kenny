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

/**
 * What each verdict says, in the words a person would use for it.
 *
 * The keys are the server's vocabulary (`toolloop.TRIAGE_VERDICTS`) and the
 * values are the reader's. Nobody outside this repository knows what a
 * "phantom" is, and a label that has to be learned before the finding under it
 * can be read is a label that teaches the machinery instead of the problem.
 */
const VERDICT_LABELS: Record<string, string> = {
  phantom: 'NO PROBLEM FOUND',
  benign_known: 'KNOWN AND HARMLESS',
  resolved_itself: 'ALREADY OVER',
  actionable: 'NEEDS ACTION',
  inconclusive: 'UNCLEAR',
}

export function verdictLabel(verdict: string): string {
  // A verdict this build has never heard of reads as unclear — the same
  // direction `verdictTone` errs in, and for the same reason: a word from a
  // newer server must not be dressed up as a conclusion by this one. Never the
  // raw token, which would put the vocabulary back on screen.
  return VERDICT_LABELS[verdict] ?? 'UNCLEAR'
}

/** A suppression an investigation proposed — a suggestion, never a rule. */
export interface SuppressionSuggestion {
  source: string
  event_id: number
}

/**
 * The parts of a triage verdict the timeline renders.
 *
 * Deliberately not all of them: the trail also records *why* the server did or
 * did not act on a verdict (`not_resolved_because`), which is a fact about
 * kenny's own machinery rather than about the machine the ticket is for. It
 * stays in the trail, where an operator asking that question will find it.
 */
export interface TriageFinding {
  verdict: string
  finding: string
  evidence: string
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
  return {
    verdict,
    finding: typeof fields?.finding === 'string' ? fields.finding : '',
    evidence: typeof fields?.evidence === 'string' ? fields.evidence : '',
    suggestion: asSuggestion(fields?.suppression_suggestion),
  }
}

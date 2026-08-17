import type { Kpi, Severity } from '../../api/types'
import { severityColor } from '../../components/tone'

/**
 * Splits the server's `verdict_sentence` prose into up to two display lines,
 * matching the prototype's `<br>`-separated two-line headline (e.g. "Two
 * machines need attention.<br>The other four are quiet."). The server sends
 * one string; the sentence boundary (first ". ") is the only signal we have
 * for where the prototype's line break falls. A single-sentence verdict (the
 * all-quiet case) renders on one line — there's nothing to split.
 */
export function splitVerdict(sentence: string): [string, string | null] {
  const trimmed = sentence.trim()
  const boundary = trimmed.indexOf('. ')
  if (boundary === -1) return [trimmed, null]
  return [trimmed.slice(0, boundary + 1), trimmed.slice(boundary + 2).trim() || null]
}

/** `TODAY · SATURDAY, AUGUST 16`, sourced from the response's own `generated_at`. */
export function todayEyebrow(generatedAt: string): string {
  const date = new Date(generatedAt)
  if (Number.isNaN(date.getTime())) return 'TODAY'
  const formatted = new Intl.DateTimeFormat('en-US', {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
  }).format(date)
  return `TODAY · ${formatted.toUpperCase()}`
}

/**
 * A KPI's number colour: neutral text for an `ok` reading (the prototype's
 * ONLINE/APP UPDATES/FAILED UPDATES=0 all render in plain text-body), the
 * matching severity colour otherwise. `Kpi.severity` is server-computed —
 * never re-derive it from the value here.
 */
export function kpiColor(severity: Severity): string {
  return severity === 'ok' ? 'var(--text-body)' : severityColor(severity)
}

/** `k.value` as a plain integer string — the frozen `Kpi.value` is a single number. */
export function kpiValue(kpi: Kpi): string {
  return String(kpi.value)
}

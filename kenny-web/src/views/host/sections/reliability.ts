import type { ReliabilityEvent } from '../types'

/**
 * The severity the LLM categoriser assigned a group (`docs/protocol.md`,
 * reliability). Distinct from the Windows `level` on the same row: `level` is
 * what the event log recorded, `severity` is what it was judged to mean. A
 * `critical` level with a `benign` severity is the ordinary case — a driver that
 * logs loudly and harms nothing — and showing only the first is why a quiet
 * machine can look alarming.
 */
export type EventSeverity = 'benign' | 'notable' | 'serious' | 'unknown'

const SEVERITY_RANK: Record<EventSeverity, number> = { serious: 3, notable: 2, unknown: 1, benign: 0 }

/** Events the categoriser has not annotated yet fall in here rather than being dropped. */
export const UNCATEGORISED = 'uncategorised'

export interface EventGroup {
  /** The friendly category, or `UNCATEGORISED`. */
  category: string
  events: ReliabilityEvent[]
  /** Summed occurrences, which is what the groups are ordered by. */
  total: number
  /** The worst severity in the group — what the group header has to answer for. */
  worst: EventSeverity
}

export function severityOf(event: ReliabilityEvent): EventSeverity {
  return event.severity ?? 'unknown'
}

/**
 * Bundle events by the categoriser's `category`, loudest group first.
 *
 * Ordering is by total occurrences, not by worst severity: the heatmap beside it
 * is a volume view, and a list ordered differently from the grid it explains
 * makes the two impossible to read together. Severity is carried on the badge,
 * where it does not have to compete with volume for the same axis.
 */
export function groupByCategory(events: ReliabilityEvent[]): EventGroup[] {
  const byCategory = new Map<string, ReliabilityEvent[]>()
  for (const ev of events) {
    const key = ev.category?.trim() || UNCATEGORISED
    const bucket = byCategory.get(key)
    if (bucket) bucket.push(ev)
    else byCategory.set(key, [ev])
  }
  return [...byCategory.entries()]
    .map(([category, list]) => ({
      category,
      events: [...list].sort((a, b) => b.count - a.count),
      total: list.reduce((sum, ev) => sum + ev.count, 0),
      worst: list.reduce<EventSeverity>(
        (worst, ev) => (SEVERITY_RANK[severityOf(ev)] > SEVERITY_RANK[worst] ? severityOf(ev) : worst),
        'benign',
      ),
    }))
    .sort((a, b) => b.total - a.total || a.category.localeCompare(b.category))
}

export interface Heatmap {
  /** ISO dates, oldest first — the union of every group's `by_day` keys. */
  days: string[]
  rows: { category: string; counts: number[]; total: number }[]
  /** The busiest single cell, for scaling the shading. 0 when there is nothing to show. */
  peak: number
}

/**
 * Fold the per-event `by_day` histograms into one category × day grid.
 *
 * The days axis is the union of every histogram's keys rather than a fixed
 * window: `window_days` is what the collector asked for, but a day on which
 * nothing happened is simply absent from the payload, and inventing columns for
 * days no event mentions would imply a reading that was never taken.
 */
export function buildHeatmap(groups: EventGroup[]): Heatmap {
  const days = new Set<string>()
  for (const group of groups) {
    for (const ev of group.events) {
      for (const day of Object.keys(ev.by_day ?? {})) days.add(day)
    }
  }
  const sortedDays = [...days].sort()
  let peak = 0
  const rows = groups.map((group) => {
    const counts = sortedDays.map((day) =>
      group.events.reduce((sum, ev) => sum + (ev.by_day?.[day] ?? 0), 0),
    )
    for (const c of counts) if (c > peak) peak = c
    return { category: group.category, counts, total: counts.reduce((a, b) => a + b, 0) }
  })
  return { days: sortedDays, rows, peak }
}

/** `2026-06-27` -> `06-27`; the year is constant across a 7-day window and only costs width. */
export function shortDay(iso: string): string {
  return iso.length === 10 ? iso.slice(5) : iso
}

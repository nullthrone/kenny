import { describe, expect, it } from 'vitest'
import type { ReliabilityEvent, ReliabilityPattern } from '../types'
import {
  UNCATEGORISED,
  activityLabel,
  buildHeatmap,
  groupByCategory,
  patternByKey,
  patternKey,
  severityOf,
  shortDay,
} from './reliability'

function event(over: Partial<ReliabilityEvent> = {}): ReliabilityEvent {
  return {
    source: 'disk',
    event_id: 7,
    level: 'error',
    count: 1,
    sample: 'the disk controller reported an error',
    last_seen: '2026-06-28T10:00:00Z',
    by_day: {},
    ...over,
  }
}

/**
 * `by_day`, `category` and `severity` all arrive on every reliability event
 * (`docs/protocol.md`) and were all declared on the client type — and none of
 * the three was rendered through 2.2.0. These are the shapes the modal builds
 * out of them.
 */
describe('groupByCategory', () => {
  it('bundles events under the categoriser label', () => {
    const groups = groupByCategory([
      event({ event_id: 1, category: 'storage' }),
      event({ event_id: 2, category: 'storage' }),
      event({ event_id: 3, category: 'graphics' }),
    ])

    // storage totals 2 occurrences across two patterns, graphics 1.
    expect(groups.map((g) => g.category)).toEqual(['storage', 'graphics'])
    expect(groups.find((g) => g.category === 'storage')?.events).toHaveLength(2)
  })

  it('orders groups by total occurrences, matching the heatmap beside them', () => {
    const groups = groupByCategory([
      event({ event_id: 1, category: 'quiet', count: 2 }),
      event({ event_id: 2, category: 'loud', count: 40 }),
    ])

    expect(groups.map((g) => g.category)).toEqual(['loud', 'quiet'])
    expect(groups[0].total).toBe(40)
  })

  it('keeps an uncategorised event rather than dropping it', () => {
    const groups = groupByCategory([event({ category: undefined }), event({ event_id: 8, category: '  ' })])

    expect(groups).toHaveLength(1)
    expect(groups[0].category).toBe(UNCATEGORISED)
    expect(groups[0].events).toHaveLength(2)
  })

  it('carries the worst severity in a group up to its header', () => {
    const groups = groupByCategory([
      event({ event_id: 1, category: 'storage', severity: 'benign' }),
      event({ event_id: 2, category: 'storage', severity: 'serious' }),
      event({ event_id: 3, category: 'storage', severity: 'notable' }),
    ])

    expect(groups[0].worst).toBe('serious')
  })

  it('treats an unannotated event as unknown, never as benign', () => {
    expect(severityOf(event())).toBe('unknown')
    expect(groupByCategory([event({ category: 'storage' })])[0].worst).toBe('unknown')
  })

  it('sorts the loudest pattern to the top within a group', () => {
    const groups = groupByCategory([
      event({ event_id: 1, category: 'storage', count: 3 }),
      event({ event_id: 2, category: 'storage', count: 30 }),
    ])

    expect(groups[0].events.map((e) => e.event_id)).toEqual([2, 1])
  })
})

describe('buildHeatmap', () => {
  it('sums each category across the union of days the events mention', () => {
    const heatmap = buildHeatmap(
      groupByCategory([
        event({ event_id: 1, category: 'storage', by_day: { '2026-06-27': 10, '2026-06-28': 2 } }),
        event({ event_id: 2, category: 'storage', by_day: { '2026-06-28': 3 } }),
        event({ event_id: 3, category: 'graphics', by_day: { '2026-06-29': 1 } }),
      ]),
    )

    expect(heatmap.days).toEqual(['2026-06-27', '2026-06-28', '2026-06-29'])
    const storage = heatmap.rows.find((r) => r.category === 'storage')
    expect(storage?.counts).toEqual([10, 5, 0])
    expect(heatmap.peak).toBe(10)
  })

  /**
   * A day nothing happened on is absent from the payload. Inventing a column for
   * every day in `window_days` would imply a reading that was never taken.
   */
  it('adds no column for a day no event mentions', () => {
    const heatmap = buildHeatmap(groupByCategory([event({ by_day: { '2026-06-28': 1 } })]))
    expect(heatmap.days).toEqual(['2026-06-28'])
  })

  it('is empty, not broken, when no event carries a histogram', () => {
    const heatmap = buildHeatmap(groupByCategory([event({ category: 'storage' })]))
    expect(heatmap.days).toEqual([])
    expect(heatmap.peak).toBe(0)
  })

  it('handles no events at all', () => {
    expect(buildHeatmap([])).toEqual({ days: [], rows: [], peak: 0 })
  })
})

describe('shortDay', () => {
  it('drops the year, which is constant across the window', () => {
    expect(shortDay('2026-06-27')).toBe('06-27')
  })

  it('leaves anything that is not an ISO date alone', () => {
    expect(shortDay('yesterday')).toBe('yesterday')
  })
})

/**
 * `details.patterns` is the health rule's own activity record per pattern
 * (ADR-0058). The console joins it onto the event cards and labels it; every
 * threshold behind `active` / `recurring` / `burst` stays in `health_rules.py`.
 */
function pattern(over: Partial<ReliabilityPattern> = {}): ReliabilityPattern {
  return {
    source: 'disk',
    event_id: 7,
    level: 'error',
    count: 1,
    severity: 'unknown',
    category: null,
    cause: null,
    user_impact: 'unknown',
    symptom: '',
    classification_state: 'unclassified',
    scores: false,
    suppressed: false,
    active_days: 1,
    first_day: '2026-06-28',
    last_day: '2026-06-28',
    last_seen_age_hours: 1,
    active: true,
    recurring: false,
    burst: false,
    ...over,
  }
}

describe('patternByKey', () => {
  it('indexes patterns by (source, event_id) so an event can find its own record', () => {
    const map = patternByKey({ patterns: [pattern({ source: 'disk', event_id: 51 }), pattern({ source: 'App', event_id: 1000 })], window_days: 7 })

    expect(map.get(patternKey(event({ source: 'disk', event_id: 51 })))?.source).toBe('disk')
    expect(map.get(patternKey(event({ source: 'App', event_id: 1000 })))?.event_id).toBe(1000)
    expect(map.get(patternKey(event({ source: 'nope', event_id: 1 })))).toBeUndefined()
  })

  it('tolerates a section without details, or malformed ones', () => {
    expect(patternByKey(undefined).size).toBe(0)
    expect(patternByKey({ patterns: 'nope' }).size).toBe(0)
    expect(patternByKey({ patterns: [null, 3] }).size).toBe(0)
  })
})

describe('activityLabel', () => {
  it('names an active, recurring pattern with its persistence over the window', () => {
    expect(activityLabel(pattern({ active: true, recurring: true, active_days: 5 }), 7)).toEqual({
      label: 'ACTIVE · 5/7 DAYS',
      tone: 'active',
    })
  })

  it('calls a pattern seen recently but only once so far new, not active', () => {
    expect(activityLabel(pattern({ active: true, recurring: false }), 7)).toEqual({ label: 'NEW', tone: 'active' })
  })

  it('dates a burst and a one-off by the day they happened', () => {
    expect(activityLabel(pattern({ active: false, recurring: true, burst: true, last_day: '2026-06-21' }), 7)).toEqual({
      label: 'BURST · 06-21',
      tone: 'quiet',
    })
    expect(activityLabel(pattern({ active: false, recurring: false, last_day: '2026-06-24' }), 7)).toEqual({
      label: 'ONE-OFF · 06-24',
      tone: 'quiet',
    })
  })

  it('says since when a recurring pattern has been quiet', () => {
    expect(activityLabel(pattern({ active: false, recurring: true, burst: false, last_day: '2026-06-25' }), 7)).toEqual({
      label: 'QUIET SINCE · 06-25',
      tone: 'quiet',
    })
  })
})

describe('groupByCategory with activity', () => {
  it('sorts the still-happening pattern above a louder one that went quiet', () => {
    const patterns = patternByKey({
      patterns: [
        pattern({ source: 'disk', event_id: 1, active: false }),
        pattern({ source: 'disk', event_id: 2, active: true }),
      ],
      window_days: 7,
    })
    const groups = groupByCategory(
      [event({ event_id: 1, category: 'storage', count: 80 }), event({ event_id: 2, category: 'storage', count: 3 })],
      patterns,
    )

    expect(groups[0].events.map((e) => e.event_id)).toEqual([2, 1])
    // Group ordering is still by volume, and the total is untouched.
    expect(groups[0].total).toBe(83)
  })
})

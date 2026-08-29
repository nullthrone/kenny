import { describe, expect, it } from 'vitest'
import type { ReliabilityEvent } from '../types'
import { UNCATEGORISED, buildHeatmap, groupByCategory, severityOf, shortDay } from './reliability'

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

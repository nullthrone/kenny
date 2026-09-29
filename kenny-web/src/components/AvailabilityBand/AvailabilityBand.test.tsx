import { fireEvent, render, screen } from '@testing-library/react'
import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { AgentAvailability } from '../../views/host/types'
import AvailabilityBand from './AvailabilityBand'
import { dayTicks, formatDuration } from './geometry'

// jsdom has no PointerEvent, so fireEvent.pointerMove would fall back to a
// bare Event and drop `clientX` — the one field the band reads.
beforeAll(() => {
  if (typeof window.PointerEvent === 'undefined') {
    class PointerEventShim extends MouseEvent {}
    window.PointerEvent = PointerEventShim as unknown as typeof PointerEvent
  }
})

/** Two days: an approx. online day, then an 8 h 26 m outage, a stretch
 * where kenny itself was down, and online again; one reboot in between. */
const DATA: AgentAvailability = {
  agent_id: 'pc',
  online: true,
  window: { start: '2026-09-01T00:00:00Z', end: '2026-09-03T00:00:00Z' },
  segments: [
    { start: '2026-09-01T00:00:00Z', end: '2026-09-02T00:00:00Z', state: 'online', approx: true },
    { start: '2026-09-02T00:00:00Z', end: '2026-09-02T08:26:00Z', state: 'offline', approx: false },
    { start: '2026-09-02T08:26:00Z', end: '2026-09-02T12:00:00Z', state: 'unknown', approx: false },
    { start: '2026-09-02T12:00:00Z', end: '2026-09-03T00:00:00Z', state: 'online', approx: false },
  ],
  boots: ['2026-09-02T09:00:00Z'],
  online_pct: 82.4,
  totals: { online_secs: 129_600, offline_secs: 30_360, unknown_secs: 12_840 },
  ledger_since: '2026-09-02T00:00:00Z',
}

function renderBand(overrides: Partial<Parameters<typeof AvailabilityBand>[0]> = {}) {
  const onDaysChange = vi.fn()
  const utils = render(<AvailabilityBand data={DATA} days={30} onDaysChange={onDaysChange} {...overrides} />)
  return { ...utils, onDaysChange }
}

/** The plot as 1000px wide from x=0, so a clientX is a per-mille of the window. */
function plotAt1000px() {
  const plot = screen.getByTestId('availability-plot')
  vi.spyOn(plot, 'getBoundingClientRect').mockReturnValue({
    left: 0, top: 0, right: 1000, bottom: 58, width: 1000, height: 58, x: 0, y: 0, toJSON: () => ({}),
  })
  return plot
}

function segmentRects(container: HTMLElement) {
  return [...container.querySelectorAll('g[data-state]')].map((g) => ({
    state: g.getAttribute('data-state'),
    approx: g.getAttribute('data-approx') === 'true',
    fill: g.querySelector('rect[data-segment]')!.getAttribute('fill'),
    x: parseFloat(g.querySelector('rect[data-segment]')!.getAttribute('x')!),
    width: parseFloat(g.querySelector('rect[data-segment]')!.getAttribute('width')!),
    hatched: g.querySelector('rect[data-hatch]') !== null,
  }))
}

describe('AvailabilityBand geometry', () => {
  it('places each segment by time: half the window is half the width', () => {
    const { container } = renderBand()
    const rects = segmentRects(container)
    expect(rects).toHaveLength(4)
    expect(rects[0].x).toBe(0)
    expect(rects[0].width).toBeCloseTo(50, 5)
    expect(rects[1].x).toBeCloseTo(50, 5)
    expect(rects[1].width).toBeCloseTo((8 * 60 + 26) / (48 * 60) * 100, 5)
    expect(rects[3].x + rects[3].width).toBeCloseTo(100, 5)
  })

  it('draws unknown in its own colour and hatches only the approx. stretch', () => {
    const { container } = renderBand()
    const [approx, offline, unknown, online] = segmentRects(container)
    expect(new Set([approx.fill, offline.fill, unknown.fill]).size).toBe(3)
    expect(online.fill).toBe(approx.fill)
    expect(unknown.fill).toBe('var(--border-mid)')
    expect(approx).toMatchObject({ approx: true, hatched: true })
    expect([offline, unknown, online].every((r) => !r.hatched)).toBe(true)
    // The hatch references this instance's own pattern.
    const hatch = container.querySelector('rect[data-hatch]')!.getAttribute('fill')!
    const id = hatch.match(/^url\(#(.+)\)$/)![1]
    expect(container.querySelector(`pattern#${id}`)).not.toBeNull()
  })

  it('gives two bands on one page distinct hatch patterns', () => {
    const { container } = render(
      <>
        <AvailabilityBand data={DATA} days={30} onDaysChange={() => {}} />
        <AvailabilityBand data={DATA} days={30} onDaysChange={() => {}} />
      </>,
    )
    const ids = [...container.querySelectorAll('pattern')].map((p) => p.id)
    expect(new Set(ids).size).toBe(2)
  })

  it('ticks each reboot and marks where exact recording starts', () => {
    const { container } = renderBand()
    const boots = container.querySelectorAll('g[data-boot] line')
    expect(boots).toHaveLength(1)
    expect(parseFloat(boots[0].getAttribute('x1')!)).toBeCloseTo(50 + (9 / 48) * 100, 5)
    const ledger = container.querySelector('g[data-ledger]')!
    expect(parseFloat(ledger.querySelector('line')!.getAttribute('x1')!)).toBeCloseTo(50, 5)
    expect(ledger.textContent).toMatch(/^exact since /)
  })

  it('leaves the ledger mark out when exact recording covers the whole window', () => {
    const { container } = renderBand({ data: { ...DATA, ledger_since: '2026-08-01T00:00:00Z' } })
    expect(container.querySelector('g[data-ledger]')).toBeNull()
  })
})

describe('AvailabilityBand header', () => {
  it('names the window and the percentage, and summarises the band for a screen reader', () => {
    renderBand()
    expect(screen.getByText('AVAILABILITY · 30 DAYS')).toBeInTheDocument()
    expect(screen.getByTestId('availability-pct')).toHaveTextContent('82.4 %')
    expect(screen.getByRole('img', { name: 'Availability over 30 days: 82.4 %, 1 outage, 1 reboot' })).toBeInTheDocument()
  })

  it('shows a dash when no time in the window is known', () => {
    renderBand({ data: { ...DATA, online_pct: null } })
    expect(screen.getByTestId('availability-pct')).toHaveTextContent('—')
    expect(screen.getByRole('img', { name: /^Availability over 30 days: —,/ })).toBeInTheDocument()
  })

  it('switches the window from the toggle', () => {
    const { onDaysChange } = renderBand()
    expect(screen.getByRole('button', { name: '30 D' })).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(screen.getByRole('button', { name: '7 D' }))
    expect(onDaysChange).toHaveBeenCalledWith(7)
  })

  it('says it is loading, failed, or has nothing yet — without drawing a band', () => {
    const { rerender } = renderBand({ data: undefined })
    expect(screen.getByText('Reading the last 30 days…')).toBeInTheDocument()
    rerender(<AvailabilityBand days={30} onDaysChange={() => {}} error="boom" />)
    expect(screen.getByText('Could not load availability: boom')).toBeInTheDocument()
    rerender(<AvailabilityBand data={{ ...DATA, segments: [] }} days={30} onDaysChange={() => {}} />)
    expect(screen.getByText('No availability recorded yet.')).toBeInTheDocument()
    expect(screen.queryByTestId('availability-plot')).toBeNull()
  })
})

describe('AvailabilityBand tooltip', () => {
  it('names the state, span and duration of the segment under the pointer', () => {
    renderBand()
    fireEvent.pointerMove(plotAt1000px(), { clientX: 600 })
    expect(screen.getByTestId('availability-tooltip').textContent).toMatch(/^offline · .+ – .+ · 8 h 26 m$/)
  })

  it('flags a reconstructed segment as approx.', () => {
    renderBand()
    fireEvent.pointerMove(plotAt1000px(), { clientX: 250 })
    expect(screen.getByTestId('availability-tooltip').textContent).toMatch(/^online · .+ · 1 d · approx\.$/)
  })

  it('names a reboot when the pointer is on its tick', () => {
    renderBand()
    // The reboot sits at 687.5 px; 690 is within reach of it.
    fireEvent.pointerMove(plotAt1000px(), { clientX: 690 })
    expect(screen.getByTestId('availability-tooltip').textContent).toMatch(/^reboot · \S+ \d{2}:\d{2}$/)
  })

  it('goes away when the pointer leaves', () => {
    renderBand()
    const plot = plotAt1000px()
    fireEvent.pointerMove(plot, { clientX: 600 })
    fireEvent.pointerLeave(plot)
    expect(screen.queryByTestId('availability-tooltip')).toBeNull()
  })
})

describe('availability formatting', () => {
  it('formats durations compactly', () => {
    expect(formatDuration(30)).toBe('< 1 m')
    expect(formatDuration(12 * 60)).toBe('12 m')
    expect(formatDuration(8 * 3600 + 26 * 60)).toBe('8 h 26 m')
    expect(formatDuration(3 * 86_400 + 4 * 3600)).toBe('3 d 4 h')
  })

  it('labels every day of a week, and every fifth day of a month ending on the latest', () => {
    const end = new Date(2026, 8, 29, 15, 0).getTime()
    const week = dayTicks({ startMs: end - 7 * 86_400_000, endMs: end })
    expect(week).toHaveLength(7)
    expect(week.every((t) => t.labelled)).toBe(true)

    const month = dayTicks({ startMs: end - 30 * 86_400_000, endMs: end })
    expect(month).toHaveLength(30)
    expect(month.filter((t) => t.labelled)).toHaveLength(6)
    expect(month[month.length - 1].labelled).toBe(true)
  })
})

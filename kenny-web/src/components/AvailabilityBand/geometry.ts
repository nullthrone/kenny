import type { AvailabilitySegment } from '../../views/host/types'
import { formatAvailabilityPct } from '../availability'

/**
 * Pure layout and wording for `AvailabilityBand` — kept out of the component
 * so the geometry is testable without a layout engine. Every x is a
 * percentage of the window's width (0..100): the band's SVG positions its
 * marks with `%` lengths, so it scales to its card without a viewBox — and
 * without the non-uniform stretch that would skew the approx. hatch.
 */

export interface TimeWindow {
  startMs: number
  endMs: number
}

export interface PlacedSegment {
  segment: AvailabilitySegment
  startMs: number
  endMs: number
  /** Left edge, % of the window. */
  x: number
  /** Width, % of the window. */
  width: number
}

export interface DayTick {
  ms: number
  x: number
  /** Only some days are labelled on a long window; the rest get a bare tick. */
  labelled: boolean
}

/** Position of an instant along the window, 0..100 (clamped). */
export function xOf(ms: number, w: TimeWindow): number {
  const span = w.endMs - w.startMs
  if (!(span > 0)) return 0
  return Math.min(100, Math.max(0, ((ms - w.startMs) / span) * 100))
}

/** Segments that parse and overlap the window, positioned along it. */
export function placeSegments(segments: AvailabilitySegment[], w: TimeWindow): PlacedSegment[] {
  return segments.flatMap((segment) => {
    const startMs = Date.parse(segment.start)
    const endMs = Date.parse(segment.end)
    if (Number.isNaN(startMs) || Number.isNaN(endMs) || endMs <= startMs) return []
    const x = xOf(startMs, w)
    const width = xOf(endMs, w) - x
    return width > 0 ? [{ segment, startMs, endMs, x, width }] : []
  })
}

/**
 * One tick per local midnight inside the window. A window of up to a week
 * labels every day; a longer one labels every fifth, counted back from the
 * most recent midnight so the day nearest "now" always carries its date.
 */
export function dayTicks(w: TimeWindow): DayTick[] {
  const ticks: DayTick[] = []
  const d = new Date(w.startMs)
  d.setHours(24, 0, 0, 0)
  while (d.getTime() < w.endMs && ticks.length < 400) {
    ticks.push({ ms: d.getTime(), x: xOf(d.getTime(), w), labelled: false })
    d.setDate(d.getDate() + 1)
    d.setHours(0, 0, 0, 0)
  }
  const spanDays = (w.endMs - w.startMs) / 86_400_000
  const step = spanDays <= 7.5 ? 1 : 5
  for (let i = ticks.length - 1, n = 0; i >= 0; i--, n++) {
    ticks[i].labelled = n % step === 0
  }
  return ticks
}

/** A day on the axis: `29.09` in de-DE, the viewer's own order elsewhere. */
export function formatDay(ms: number, locale?: string): string {
  return new Date(ms).toLocaleDateString(locale, { day: '2-digit', month: '2-digit' })
}

/** An instant in a tooltip: `28.09 22:14` (viewer's locale and zone). */
export function formatInstant(ms: number, locale?: string): string {
  const time = new Date(ms).toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit', hour12: false })
  return `${formatDay(ms, locale)} ${time}`
}

/** `8 h 26 m`, `3 d 4 h`, `12 m`; anything under a minute is `< 1 m`. */
export function formatDuration(seconds: number): string {
  if (!(seconds >= 60)) return '< 1 m'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} m`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) {
    const m = minutes % 60
    return m ? `${hours} h ${m} m` : `${hours} h`
  }
  const days = Math.floor(hours / 24)
  const h = hours % 24
  return h ? `${days} d ${h} h` : `${days} d`
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? '' : 's'}`
}

/** The band's accessible name — everything the picture says, in one line. */
export function availabilitySummary(days: number, pct: number | null, outages: number, reboots: number): string {
  return `Availability over ${plural(days, 'day')}: ${formatAvailabilityPct(pct)}, ${plural(outages, 'outage')}, ${plural(reboots, 'reboot')}`
}

export function describeSegment(p: PlacedSegment, locale?: string): string {
  const parts = [
    p.segment.state,
    `${formatInstant(p.startMs, locale)} – ${formatInstant(p.endMs, locale)}`,
    formatDuration((p.endMs - p.startMs) / 1000),
  ]
  if (p.segment.approx) parts.push('approx.')
  return parts.join(' · ')
}

export function describeBoot(ms: number, locale?: string): string {
  return `reboot · ${formatInstant(ms, locale)}`
}

export type BandHit = { kind: 'boot'; ms: number; x: number } | { kind: 'segment'; placed: PlacedSegment; x: number }

/**
 * What sits under the pointer at `x` (% of the window). A reboot wins when
 * its tick lies within `bootSlop` (also %) — the tick is a hairline, and
 * asking the reader to land on it exactly would hide it behind the segment.
 */
export function hitTest(x: number, placed: PlacedSegment[], bootXs: { ms: number; x: number }[], bootSlop: number): BandHit | null {
  let nearest: { ms: number; x: number } | null = null
  for (const b of bootXs) {
    if (Math.abs(b.x - x) <= bootSlop && (!nearest || Math.abs(b.x - x) < Math.abs(nearest.x - x))) nearest = b
  }
  if (nearest) return { kind: 'boot', ms: nearest.ms, x: nearest.x }
  const found = placed.find((p) => x >= p.x && x <= p.x + p.width)
  return found ? { kind: 'segment', placed: found, x } : null
}

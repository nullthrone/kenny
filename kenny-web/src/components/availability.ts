import type { AvailabilityState } from '../views/host/types'

/**
 * Availability state → the CSS custom property that colours it, shared by
 * the host page's band and the fleet cards' 7-day strip so the two read as
 * one scale. `unknown` (the kenny server was down, or the host not enrolled
 * yet) is `--border-mid`: a neutral that stays visible on the card surface in
 * both themes without competing with the two states that mean something.
 */
export function availabilityColor(state: AvailabilityState): string {
  switch (state) {
    case 'online':
      return 'var(--ok)'
    case 'offline':
      return 'var(--danger)'
    case 'unknown':
      return 'var(--border-mid)'
  }
}

/**
 * One hourly strip cell (online fraction of the hour's known time, or null
 * when none of it is known) → its colour. Only a fully online or fully
 * offline hour gets the pure state colour; any mix reads as `--warn`, so a
 * short drop inside an otherwise online hour is still visible at a glance.
 */
export function availabilityCellColor(value: number | null): string {
  if (value === null) return availabilityColor('unknown')
  if (value >= 0.999) return availabilityColor('online')
  if (value <= 0.001) return availabilityColor('offline')
  return 'var(--warn)'
}

/** `99.2` → `99.2 %`; `null` (no known time in the window) → `—`. */
export function formatAvailabilityPct(pct: number | null | undefined): string {
  if (pct == null || !Number.isFinite(pct)) return '—'
  return `${pct.toFixed(1)} %`
}

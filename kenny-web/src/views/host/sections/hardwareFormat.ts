import type { GpuPcie } from '../types'

/**
 * Pure display helpers shared by the hardware-health bodies (`DiskBody`,
 * `HardwareErrorsBody`, `GpuBody`, `FansBody`). Every reading in those
 * sections is nullable ("the source does not report it" — unknown, never zero),
 * so each helper takes `null | undefined` and answers with a dash rather than
 * a made-up number.
 */

export const DASH = '—'

/** A finite number, else null — so a missing, null or NaN reading is one case. */
export function finite(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** Fixed locale so a count reads the same in every browser (and in tests). */
export function formatCount(value: number | null | undefined): string {
  const n = finite(value)
  return n === null ? DASH : n.toLocaleString('en-US')
}

export function formatPercent(value: number | null | undefined): string {
  const n = finite(value)
  return n === null ? DASH : `${formatCount(n)}%`
}

export function formatTemp(current: number | null | undefined, max?: number | null): string {
  const c = finite(current)
  const m = finite(max)
  if (c === null && m === null) return DASH
  if (c === null) return `max ${m} °C`
  return m === null ? `${c} °C` : `${c} °C (max ${m} °C)`
}

/** "1,520 h" below two days, "1,520 h (63 d)" above — the days are the figure people read. */
export function formatPowerOnHours(hours: number | null | undefined): string {
  const h = finite(hours)
  if (h === null) return DASH
  return h >= 48 ? `${formatCount(h)} h (${Math.round(h / 24)} d)` : `${formatCount(h)} h`
}

/** Watts with one decimal at most, e.g. "38.5 W" / "320 W". */
export function formatWatts(value: number | null | undefined): string {
  const w = finite(value)
  return w === null ? DASH : `${Number.isInteger(w) ? w : w.toFixed(1)} W`
}

/** "Gen 1 ×16 (max Gen 4 ×16)"; a half-reported link keeps what it has. */
export function formatPcieLink(pcie: GpuPcie | null | undefined): string {
  if (!pcie) return DASH
  const link = (gen: number | null, width: number | null) => {
    const parts = [gen !== null ? `Gen ${gen}` : null, width !== null ? `×${width}` : null].filter(Boolean)
    return parts.length ? parts.join(' ') : null
  }
  const now = link(finite(pcie.gen_current), finite(pcie.width_current))
  const max = link(finite(pcie.gen_max), finite(pcie.width_max))
  if (now && max) return `${now} (max ${max})`
  if (now) return now
  if (max) return `max ${max}`
  return DASH
}

/** True when a counter is a number above zero. `null` is unknown, so not an alert. */
export function isNonZero(value: number | null | undefined): boolean {
  const n = finite(value)
  return n !== null && n > 0
}

/** A `by_day` map's keys (UTC `YYYY-MM-DD`), oldest first. */
export function sortedDays(byDay: Record<string, number> | null | undefined): string[] {
  return Object.keys(byDay ?? {}).sort()
}

/** Mean of the finite values, or null when there are none. */
export function mean(values: readonly number[]): number | null {
  const v = values.filter((x) => Number.isFinite(x))
  if (v.length === 0) return null
  return v.reduce((a, b) => a + b, 0) / v.length
}

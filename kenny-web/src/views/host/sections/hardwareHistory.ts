import type { HardwareDevice, HardwareForecast, HardwareSeriesPoint, HardwareTrends } from '../types'

/**
 * Lookups into the long-lived device history (`useHardwareTrends`). Every one
 * takes the whole `hardware` block as `null | undefined` and answers with
 * "nothing" when it is absent, so a body on an older server renders unchanged.
 */

export function findDevice(hardware: HardwareTrends | null | undefined, ...keys: (string | null | undefined)[]): HardwareDevice | null {
  if (!hardware) return null
  for (const key of keys) {
    if (!key) continue
    const hit = hardware.devices.find((d) => d.device_key === key)
    if (hit) return hit
  }
  return null
}

/** The device's series for `metric` when it has at least two points to draw. */
export function drawable(device: HardwareDevice | null, metric: string): HardwareSeriesPoint[] | null {
  const points = device?.series[metric]
  return points && points.length >= 2 ? points : null
}

export function hasNonZero(points: HardwareSeriesPoint[] | null | undefined): boolean {
  return !!points && points.some((p) => p.value > 0)
}

export function lastValue(points: HardwareSeriesPoint[]): number {
  return points[points.length - 1].value
}

const DAY_MS = 86_400_000

/** Calendar days from the first to the last point, both included. */
export function spanDays(points: HardwareSeriesPoint[]): number {
  const first = Date.parse(points[0].day)
  const last = Date.parse(points[points.length - 1].day)
  if (!Number.isFinite(first) || !Number.isFinite(last)) return points.length
  return Math.max(1, Math.round((last - first) / DAY_MS) + 1)
}

export function forecastsFor(
  hardware: HardwareTrends | null | undefined,
  deviceKey: string | null | undefined,
  reason?: string,
): HardwareForecast[] {
  if (!hardware || !deviceKey) return []
  return hardware.forecasts.filter((f) => f.device_key === deviceKey && (reason === undefined || f.reason === reason))
}

/** "in ~143 days" / "in ~1 day"; null when the forecast carries no date. */
export function formatDaysUntil(days: number | null | undefined): string | null {
  if (typeof days !== 'number' || !Number.isFinite(days)) return null
  const n = Math.max(0, Math.round(days))
  return `in ~${n} ${n === 1 ? 'day' : 'days'}`
}

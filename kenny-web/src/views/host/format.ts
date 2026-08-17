import type { Severity } from '../../api/types'

/** Family string (`registry.Agent.os`: windows/linux/macos) → a display label.
 * Duplicated in `views/fleet/format.ts` rather than shared across the two
 * owned view slices — a ~10-line pure function is cheaper than a cross-view
 * dependency. */
export function osLabel(os: string): string {
  switch (os.toLowerCase()) {
    case 'windows':
      return 'Windows'
    case 'linux':
      return 'Linux'
    case 'macos':
      return 'macOS'
    default:
      return os || 'Unknown'
  }
}

/** Ordinal position for charting only — `ok` highest, `crit` lowest — never a
 * threshold decision. The `overall` value plotted is already the server's own
 * worst-of classification (`health_rules.worst`); this only assigns it a Y
 * position on the sparkline. */
export function severityRank(s: Severity): number {
  switch (s) {
    case 'ok':
      return 3
    case 'warn':
      return 2
    case 'crit':
      return 1
    case 'unknown':
      return 0
  }
}

/**
 * Narrows a host's free-text OS family (`windows`/`linux`/`macos`/…) down to
 * what the installer and share-link endpoints accept
 * (`distribution.py::SUPPORTED_OS = {"windows", "linux"}`). Anything else
 * falls back to `windows`, matching the server's own default when the `os`
 * query param/body field is omitted (`_req_os`, `share_link_by_name`).
 */
export function toShareOs(os: string): 'windows' | 'linux' {
  return os.toLowerCase() === 'linux' ? 'linux' : 'windows'
}

/** Binary-prefix byte formatter — disk volumes and directory sizes. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let value = bytes
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i++
  }
  return `${i > 0 && value < 10 ? value.toFixed(1) : Math.round(value)} ${units[i]}`
}

export function formatRelativeTime(iso: string | null | undefined): string {
  if (!iso) return 'never'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return 'never'
  const minutes = Math.round((Date.now() - then) / 60_000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  return `${days}d ago`
}

/** The wall-clock time of an ISO instant, in the viewer's own locale/zone —
 * used for "reverts at 21:00", never for the countdown itself (that reads
 * the ISO instant directly against `Date.now()`). */
export function formatClockTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

/** A forward time-until as "3h 12m" / "45m" / "any moment" — the web filter
 * schedule banner's "reverts in …" (`schedule_state().reverts_at`). Never
 * negative: a past instant reads as "any moment" rather than "-3m". */
export function formatCountdown(iso: string | null | undefined): string {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const ms = then - Date.now()
  if (ms <= 0) return 'any moment'
  const minutes = Math.round(ms / 60_000)
  if (minutes < 1) return 'under a minute'
  const hours = Math.floor(minutes / 60)
  const mins = minutes % 60
  if (hours === 0) return `${mins}m`
  if (mins === 0) return `${hours}h`
  return `${hours}h ${mins}m`
}

/**
 * Best-effort text rendering for an arbitrary telemetry field whose shape
 * this view has no specific model for — the generic-walk fallback the notes
 * describe ("the UI walks generically"). Never a health judgement, purely
 * display formatting of whatever the server already sent.
 */
export function formatGenericValue(value: unknown, depth = 0): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (typeof value === 'number') return String(value)
  if (typeof value === 'string') return value || '—'
  if (Array.isArray(value)) {
    if (value.length === 0) return '—'
    if (depth >= 2) return `${value.length} item${value.length === 1 ? '' : 's'}`
    const shown = value.slice(0, 6).map((v) => formatGenericValue(v, depth + 1))
    const extra = value.length > 6 ? `, +${value.length - 6} more` : ''
    return shown.join(', ') + extra
  }
  if (typeof value === 'object') {
    if (depth >= 2) return '{…}'
    const entries = Object.entries(value as Record<string, unknown>)
    if (entries.length === 0) return '—'
    return entries.map(([k, v]) => `${k}: ${formatGenericValue(v, depth + 1)}`).join(' · ')
  }
  return String(value)
}

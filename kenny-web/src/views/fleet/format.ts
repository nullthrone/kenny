import type { LucideIcon } from '../../components/icons'
import { AppWindow, Server } from '../../components/icons'

/** `FleetAgent.os` is the agent's lower-cased OS family (`registry.Agent.os`:
 * `windows`/`linux`/`macos`) — never a build string like "Windows 11 23H2",
 * which nothing in the wire contract carries. Windows gets the prototype's
 * `app-window` glyph; anything else (linux, macos, unknown) gets `server`. */
export function osIcon(os: string): LucideIcon {
  return os.toLowerCase() === 'windows' ? AppWindow : Server
}

/** Family string → a short display label ("Windows"/"Linux"/"macOS"/the raw value). */
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

/** `collected_at` (ISO or null) → the prototype's short relative form ("12 m", "3 h", "9 d"). */
export function relativePush(collectedAt: string | null): string {
  if (!collectedAt) return 'never'
  const then = new Date(collectedAt).getTime()
  if (Number.isNaN(then)) return 'never'
  const minutes = Math.max(0, Math.round((Date.now() - then) / 60_000))
  if (minutes < 60) return `${minutes} m`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} h`
  const days = Math.round(hours / 24)
  return `${days} d`
}

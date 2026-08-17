import type { Role } from '../api/types'

/** `thomas` → `TH`. `mia desktop` → `MD`. Matches the prototype's two-letter avatar glyph. */
export function initialsOf(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase()
  return name.slice(0, 2).toUpperCase()
}

/** `Role` → the sidebar/profile caps label. */
export function roleLabel(role: Role): string {
  return role.toUpperCase()
}

/** `InboxItem.age_seconds` → the row's compact age string ("38 m", "2 h", "1 d"), matching the prototype's copy. */
export function formatAge(ageSeconds: number): string {
  const seconds = Math.max(0, Math.round(ageSeconds))
  if (seconds < 60) return `${seconds} s`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} m`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} h`
  const days = Math.round(hours / 24)
  return `${days} d`
}

/** Strips the console route's leading `#` so it can be handed to `<Link to>` under `HashRouter`. */
export function toRoutePath(target: string): string {
  return target.startsWith('#') ? target.slice(1) : target
}

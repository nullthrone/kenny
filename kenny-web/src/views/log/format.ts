/**
 * Row timestamp for the log stream: full date and time, joined by a space
 * (`20.09.2026 19:56` in de-DE). The stream spans days, so a bare time is
 * ambiguous as soon as it scrolls past midnight.
 */
export function formatLogTimestamp(ts: string, locale?: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return '—'
  const date = d.toLocaleDateString(locale, { day: '2-digit', month: '2-digit', year: 'numeric' })
  const time = d.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit', hour12: false })
  return `${date} ${time}`
}

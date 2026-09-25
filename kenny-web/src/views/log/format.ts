/**
 * Row timestamp for the log stream: day, month and time, plus the year when the
 * row is not from the current year. The stream spans days, so a bare time is
 * ambiguous as soon as it scrolls past midnight.
 */
export function formatLogTimestamp(ts: string, now: Date = new Date(), locale?: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString(locale, {
    ...(d.getFullYear() !== now.getFullYear() && { year: 'numeric' }),
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

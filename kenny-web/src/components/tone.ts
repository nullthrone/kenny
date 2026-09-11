import type { Severity, TicketPriority } from '../api/types'

/** `Severity` → the CSS custom property that colours it, matching the prototype's palette. */
export function severityColor(severity: Severity): string {
  switch (severity) {
    case 'ok':
      return 'var(--ok)'
    case 'posture':
      return 'var(--text-muted)'
    case 'warn':
      return 'var(--warn)'
    case 'crit':
      return 'var(--danger)'
    case 'unknown':
      return 'var(--text-faint)'
  }
}

export function severityLabel(severity: Severity): string {
  switch (severity) {
    case 'ok':
      return 'HEALTHY'
    case 'posture':
      return 'POSTURE'
    case 'warn':
      return 'WARNING'
    case 'crit':
      return 'CRITICAL'
    case 'unknown':
      return 'UNKNOWN'
  }
}

/**
 * `TicketPriority` → the CSS custom property that colours its badge.
 *
 * Deliberately not exhaustive over the union: `tickets.PRIORITIES` is server
 * vocabulary, and a value added there must render dully rather than crash the
 * queue. The same applies to `priorityLabel`.
 */
export function priorityColor(priority: string): string {
  switch (priority) {
    case 'urgent':
      return 'var(--danger)'
    case 'high':
      return 'var(--warn)'
    case 'low':
      return 'var(--text-faint)'
    default:
      return 'var(--text-muted)'
  }
}

export function priorityLabel(priority: string): string {
  return (priority || 'normal').toUpperCase()
}

export type { TicketPriority }

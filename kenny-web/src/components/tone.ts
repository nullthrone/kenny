import type { InboxKind, Severity } from '../api/types'

/** `Severity` → the CSS custom property that colours it, matching the prototype's palette. */
export function severityColor(severity: Severity): string {
  switch (severity) {
    case 'ok':
      return 'var(--ok)'
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
    case 'warn':
      return 'WARNING'
    case 'crit':
      return 'CRITICAL'
    case 'unknown':
      return 'UNKNOWN'
  }
}

/**
 * `InboxKind` → default caps label + colour, matching the prototype's
 * kindColor mapping (APPROVAL=brass, TICKET=muted, ALERT=warn).
 *
 * GAP: the prototype's mock data shows a `section`-kind row with its label
 * as the section's actual severity ("CRITICAL"/"WARNING"), not the literal
 * word "SECTION" — but `InboxItem` (types.ts, frozen) carries no severity
 * field, only `kind`. There's nowhere to source that distinction from
 * honestly. The default below reads "SECTION" in `--danger`; pass
 * `label`/`tone` overrides on `<SourceBadge>` if/when the view wiring
 * this up has the real section severity from elsewhere (e.g. the
 * corresponding `HostSection`).
 */
export function inboxKindDefault(kind: InboxKind): { label: string; color: string } {
  switch (kind) {
    case 'approval':
      return { label: 'APPROVAL', color: 'var(--brass-600)' }
    case 'ticket':
      return { label: 'TICKET', color: 'var(--text-muted)' }
    case 'alert':
      return { label: 'ALERT', color: 'var(--warn)' }
    case 'section':
      return { label: 'SECTION', color: 'var(--danger)' }
  }
}

import type { InboxKind, Severity } from '../../api/types'
import { inboxKindDefault, severityColor } from '../tone'
import styles from './SourceBadge.module.css'

export interface SourceBadgeProps {
  kind: InboxKind
  /** Override the default caps label (see `inboxKindDefault`'s doc comment re: the `section`-kind gap). */
  label?: string
  /** Override the default colour with a known severity — use this for `section`/`alert` rows once the caller has the real severity. */
  tone?: Severity
  className?: string
}

/** Inbox row's kind chip: APPROVAL / TICKET / SECTION / ALERT, outlined in its tone colour. */
export default function SourceBadge({ kind, label, tone, className }: SourceBadgeProps) {
  const fallback = inboxKindDefault(kind)
  const color = tone ? severityColor(tone) : fallback.color
  return (
    <span className={`${styles.badge} kc-caps${className ? ` ${className}` : ''}`} style={{ color }}>
      {label ?? fallback.label}
    </span>
  )
}

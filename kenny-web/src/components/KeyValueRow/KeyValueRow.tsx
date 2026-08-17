import type { ReactNode } from 'react'
import styles from './KeyValueRow.module.css'

export interface KeyValueRowAction {
  label: string
  onClick: () => void
}

export interface KeyValueRowSourceBadge {
  label: string
  /** A CSS colour value, e.g. `var(--brass-600)` or `var(--text-faint)` — see `ConfigSource`'s three states. */
  color: string
}

export interface KeyValueRowProps {
  label: string
  help?: string
  /** Usually a plain string; a `ReactNode` so a value can carry its own colour/formatting (e.g. `<span style={{color:'var(--ok)'}}>enabled</span>`). Rendered in mono, `white-space:nowrap` (`kc-value`) — a value like `thomas#1121` must never break mid-token. */
  value: ReactNode
  /** The Profile-style trailing button ("CHANGE", "MANAGE", "ROTATE" …). */
  action?: KeyValueRowAction
  /** The Admin-style trailing source chip (default/env/custom — see `ConfigSource`). */
  sourceBadge?: KeyValueRowSourceBadge
  className?: string
}

/** The label / help / value / action row shared by Profile and Admin. */
export default function KeyValueRow({ label, help, value, action, sourceBadge, className }: KeyValueRowProps) {
  return (
    <div className={`${styles.row}${className ? ` ${className}` : ''}`}>
      <div className={`${styles.labelWrap} kc-cell`}>
        <div className={styles.label}>{label}</div>
        {help && <div className={styles.help}>{help}</div>}
      </div>
      <span className={`${styles.value} kc-value`}>{value}</span>
      {sourceBadge && (
        <span className={`${styles.sourceBadge} kc-caps`} style={{ color: sourceBadge.color }}>
          {sourceBadge.label}
        </span>
      )}
      {action && (
        <button type="button" className={`${styles.action} kc-btn`} onClick={action.onClick}>
          {action.label}
        </button>
      )}
    </div>
  )
}

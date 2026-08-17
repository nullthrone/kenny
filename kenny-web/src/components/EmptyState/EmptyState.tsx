import type { LucideIcon } from '../icons'
import { ICON_STROKE_WIDTH } from '../icons'
import styles from './EmptyState.module.css'

export interface EmptyStateAction {
  label: string
  onClick: () => void
}

export interface EmptyStateProps {
  icon?: LucideIcon
  title: string
  /** Nullthrone voice: state facts, no apology theater. For an error, say what happened and what to do — not "Oops!". */
  message?: string
  action?: EmptyStateAction
  className?: string
}

/**
 * A quiet fleet (`TodayResponse.items === []`) is a first-class state here,
 * not an error — use the same component for "all quiet", "nothing found",
 * and a genuine failure; only the copy changes.
 */
export default function EmptyState({ icon: Icon, title, message, action, className }: EmptyStateProps) {
  return (
    <div className={`${styles.root}${className ? ` ${className}` : ''}`}>
      {Icon && <Icon width={32} height={32} strokeWidth={ICON_STROKE_WIDTH} className={styles.icon} aria-hidden="true" />}
      <div className={styles.title}>{title}</div>
      {message && <p className={styles.message}>{message}</p>}
      {action && (
        <button type="button" className={`${styles.action} kc-btn`} onClick={action.onClick}>
          {action.label}
        </button>
      )}
    </div>
  )
}

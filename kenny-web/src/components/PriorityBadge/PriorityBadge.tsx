import { priorityColor, priorityLabel } from '../tone'
import styles from './PriorityBadge.module.css'

export interface PriorityBadgeProps {
  /** `tickets.PRIORITIES`; an unrecognised value renders dully rather than throwing. */
  priority: string
  className?: string
}

/** A queue row's badge: URGENT / HIGH / NORMAL / LOW, outlined in its tone colour. */
export default function PriorityBadge({ priority, className }: PriorityBadgeProps) {
  return (
    <span
      className={`${styles.badge} kc-caps${className ? ` ${className}` : ''}`}
      style={{ color: priorityColor(priority) }}
    >
      {priorityLabel(priority)}
    </span>
  )
}

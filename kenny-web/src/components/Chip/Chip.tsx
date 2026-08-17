import styles from './Chip.module.css'

export interface ChipProps {
  label: string
  /** Rendered in mono after the label, e.g. Inbox group counts, unstyled otherwise. */
  count?: number | string
  active?: boolean
  onClick?: () => void
  className?: string
}

/** Toggle-style filter/group chip — Inbox's group tabs, Log's kind filters. */
export default function Chip({ label, count, active = false, onClick, className }: ChipProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`${styles.chip}${active ? ` ${styles.active}` : ''}${className ? ` ${className}` : ''}`}
    >
      {label} {count !== undefined && <span className={styles.count}>{count}</span>}
    </button>
  )
}

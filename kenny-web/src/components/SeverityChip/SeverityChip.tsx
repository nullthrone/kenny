import type { Severity } from '../../api/types'
import { severityColor, severityLabel } from '../tone'
import styles from './SeverityChip.module.css'

export interface SeverityChipProps {
  severity: Severity
  /** Defaults to the generic caps label (HEALTHY/WARNING/CRITICAL/UNKNOWN). Pass the API's own text (e.g. `FleetAgent.severity_label`, `"CRITICAL · DISK"`) when you have it — never invent a threshold-derived label client-side. */
  label?: string
  /** `outline` (bordered chip — Today row, Inbox row) or `text` (bare coloured caps text — Fleet card sev line). Default `outline`. */
  variant?: 'outline' | 'text'
  className?: string
}

export default function SeverityChip({ severity, label, variant = 'outline', className }: SeverityChipProps) {
  const color = severityColor(severity)
  return (
    <span
      className={`${styles.chip} ${variant === 'outline' ? styles.outline : styles.text} kc-caps${className ? ` ${className}` : ''}`}
      style={{ color }}
    >
      {label ?? severityLabel(severity)}
    </span>
  )
}

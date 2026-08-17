import type { AdminRow } from '../types'
import EditableSettingRow from '../EditableSettingRow'
import styles from './GenericSettingsSection.module.css'

export interface GenericSettingsSectionProps {
  rows: AdminRow[]
}

/**
 * The generic renderer for any config group straight off `GET /api/settings`
 * — used for every real group that has no bespoke UI (Alerting & Digest,
 * Chat & AI, Logging, Network & Process, Operator & Agent Auth, Telemetry
 * limits, Agent distribution). Nothing here is hardcoded per-group; the
 * catalog drives it entirely.
 */
export default function GenericSettingsSection({ rows }: GenericSettingsSectionProps) {
  return (
    <div className={styles.rows}>
      {rows.map((row) => (
        <EditableSettingRow key={row.key} row={row} />
      ))}
    </div>
  )
}

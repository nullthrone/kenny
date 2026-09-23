import type { AdminRow } from '../types'
import EditableSettingRow from '../EditableSettingRow'
import styles from './GenericSettingsSection.module.css'

export interface GenericSettingsSectionProps {
  rows: AdminRow[]
}

/**
 * The generic renderer for any config group straight off `GET /api/settings`
 * — the whole section for a group with no bespoke UI (AI, Tickets, Web filter,
 * System), and the settings part of every section that has one. Nothing here is
 * hardcoded per-group; the catalog drives it entirely.
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

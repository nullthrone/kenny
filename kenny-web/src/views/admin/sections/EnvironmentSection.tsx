import KeyValueRow from '../../../components/KeyValueRow/KeyValueRow'
import EmptyState from '../../../components/EmptyState/EmptyState'
import type { AdminRow } from '../types'
import { CONFIG_SOURCE_COLOR, CONFIG_SOURCE_LABEL } from '../types'
import styles from './GenericSettingsSection.module.css'

export interface EnvironmentSectionProps {
  rows: AdminRow[]
}

/**
 * Synthetic `environment` section — every row across every group whose
 * source is `env`, read-only. Not a server group of its own; composed by
 * `settingsMap.buildEnvironmentSection` from the same catalog every other
 * section reads.
 */
export default function EnvironmentSection({ rows }: EnvironmentSectionProps) {
  if (rows.length === 0) {
    return <EmptyState title="Nothing environment-sourced" message="No setting in the catalog is currently overridden by the environment." />
  }
  return (
    <div className={styles.rows}>
      {rows.map((row) => (
        <KeyValueRow
          key={row.key}
          label={row.label}
          help={row.help}
          value={row.value === null || row.value === '' ? <span style={{ color: 'var(--text-faint)' }}>not set</span> : String(row.value)}
          sourceBadge={{ label: CONFIG_SOURCE_LABEL[row.source], color: CONFIG_SOURCE_COLOR[row.source] }}
        />
      ))}
    </div>
  )
}

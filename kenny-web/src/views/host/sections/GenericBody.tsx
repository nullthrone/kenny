import KeyValueRow from '../../../components/KeyValueRow/KeyValueRow'
import { humanizeSectionName } from '../sections'
import { formatGenericValue } from '../format'
import type { RawSection } from '../types'

export interface GenericBodyProps {
  data: RawSection
}

const HIDDEN_KEYS = new Set(['status', 'summary', 'reason'])

/** The fallback body for any section without a specialized editor:
 * `status`/`summary`/`reason` are already shown in the modal's own header
 * and rule chip, so everything else is walked generically. */
export default function GenericBody({ data }: GenericBodyProps) {
  const entries = Object.entries(data).filter(([key]) => !HIDDEN_KEYS.has(key))

  if (entries.length === 0) {
    return <p style={{ color: 'var(--text-muted)', fontSize: 'var(--text-sm)' }}>No further detail reported for this section.</p>
  }

  return (
    <div style={{ borderTop: '1px solid var(--border-strong)' }}>
      {entries.map(([key, value]) => (
        <KeyValueRow key={key} label={humanizeSectionName(key)} value={formatGenericValue(value)} />
      ))}
    </div>
  )
}

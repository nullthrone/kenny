import { Link } from 'react-router'
import type { InboxItem } from '../../api/types'
import SourceBadge from '../../components/SourceBadge/SourceBadge'
import { severityLabel } from '../../components/tone'
import ApprovalGate, { type DecisionOutcome } from '../ticket/ApprovalGate'
import { formatAge, toRoutePath } from './age'
import styles from './InboxRow.module.css'

export interface InboxRowProps {
  item: InboxItem
  onDecided: (item: InboxItem, outcome: DecisionOutcome) => void
}

/**
 * One hairline row: kind chip, title link, meta, age — and, for `kind:
 * 'approval'`, the gate rendered inline via `ApprovalGate`/`GateCard`.
 */
export default function InboxRow({ item, onDecided }: InboxRowProps) {
  // `SourceBadge`'s default label for `section` is the literal word
  // "SECTION" (tone.ts's documented gap: InboxItem carries no compound
  // severity label). Where the row does carry a real `severity`, showing
  // it (CRITICAL/WARNING) instead is truer to what the row is about.
  const badgeLabel = item.kind === 'section' && item.severity ? severityLabel(item.severity) : undefined

  return (
    <div className={`${styles.row} kc-stagger-row`}>
      <SourceBadge kind={item.kind} label={badgeLabel} tone={item.severity ?? undefined} className={styles.badge} />
      <div className={`${styles.body} kc-cell`}>
        <Link to={toRoutePath(item.target)} className={styles.title}>
          {item.title}
        </Link>
        <div className={styles.meta}>{item.meta}</div>
        {item.gate && (
          <div className={styles.gate}>
            <ApprovalGate
              approvalId={item.gate.approval_id}
              tool={item.gate.tool}
              args={item.gate.args}
              agentId={item.gate.agent_id}
              toolClass={item.gate.tool_class}
              onDecided={(outcome) => onDecided(item, outcome)}
            />
          </div>
        )}
      </div>
      <span className={styles.age}>{formatAge(item.age_seconds)}</span>
    </div>
  )
}

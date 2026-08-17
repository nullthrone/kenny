import { formatArgs } from './formatArgs'
import styles from './GateCard.module.css'

export interface GateCardProps {
  tool: string
  /**
   * Frozen at hold time — the resolved target is fixed before the gate
   * runs, so a later host switch can't retarget the call (ADR-0038).
   * Rendered VERBATIM: never truncated, reformatted, or re-serialised.
   */
  args: Record<string, unknown>
  agentId?: string | null
  toolClass?: string
  eyebrow?: string
  approveLabel?: string
  denyLabel?: string
  /** Omit to hide the third button entirely — the fleet-chat gate has only CONFIRM & RUN / CANCEL. */
  onDecideLater?: () => void
  decideLaterLabel?: string
  onApprove: () => void
  onDeny: () => void
  /** Disables all three actions while a decision is in flight. */
  busy?: boolean
  className?: string
}

/**
 * The amber state-changing confirmation card. SECURITY-CRITICAL: this is
 * the operator's only look at what approving will run before it runs.
 *
 * Used both inline (ticket detail, Inbox row) and inside a Modal (the
 * fleet-chat confirmation, which must be wrapped in
 * `<Modal dismissible={false}>` by the caller — GateCard itself has no
 * opinion on dismissibility, that's the surrounding Modal's job).
 */
export default function GateCard({
  tool,
  args,
  agentId,
  toolClass,
  eyebrow = 'STATE-CHANGING · FROZEN ARGUMENTS · NEEDS YOUR DECISION',
  approveLabel = 'APPROVE & RUN',
  denyLabel = 'DENY',
  onDecideLater,
  decideLaterLabel = 'DECIDE LATER',
  onApprove,
  onDeny,
  busy = false,
  className,
}: GateCardProps) {
  const metaParts = [toolClass, agentId].filter((p): p is string => Boolean(p))

  return (
    <div className={`${styles.card}${className ? ` ${className}` : ''}`}>
      <div className={`${styles.eyebrow} kc-caps`}>{eyebrow}</div>
      <div className={styles.call}>
        <span className={styles.tool}>{tool}</span> <span className={styles.args}>{formatArgs(args)}</span>
        {metaParts.length > 0 && <span className={styles.meta}> · {metaParts.join(' · ')}</span>}
      </div>
      <div className={`${styles.actions} kc-actions`}>
        <button type="button" className={styles.approve} onClick={onApprove} disabled={busy}>
          {approveLabel}
        </button>
        <button type="button" className={styles.deny} onClick={onDeny} disabled={busy}>
          {denyLabel}
        </button>
        {onDecideLater && (
          <button type="button" className={styles.decideLater} onClick={onDecideLater} disabled={busy}>
            {decideLaterLabel}
          </button>
        )}
      </div>
    </div>
  )
}

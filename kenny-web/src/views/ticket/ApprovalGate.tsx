import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api } from '../../api/client'
import GateCard from '../../components/GateCard/GateCard'
import type { ApprovalDecideResponse } from './types'
import { decisionMessage } from './decision'
import styles from './ApprovalGate.module.css'

export interface DecisionOutcome {
  approve: boolean
  resumed: boolean
  message: string
}

export interface ApprovalGateProps {
  approvalId: string
  tool: string
  /** Frozen at hold time — rendered verbatim by `GateCard`. Never touch this before it reaches that component. */
  args: Record<string, unknown>
  agentId?: string | null
  toolClass?: string
  eyebrow?: string
  /**
   * Fires once the decision round-trip completes. The caller owns surfacing
   * `message` (there is no shared toast primitive) and invalidating
   * whatever list/detail query should now reflect the decision.
   */
  onDecided: (outcome: DecisionOutcome) => void
  /**
   * Pass this when the gate is rendered inside a dismissible `Modal`
   * (the ticket chat's live pending gate) so "decide later" closes the
   * dialog instead. Omit for a gate that has no dialog around it (an
   * Inbox row, the ticket page's durable inline gate) — there the row
   * itself is already the real path back, so "decide later" just
   * collapses it locally rather than doing nothing.
   */
  onDecideLater?: () => void
  className?: string
}

/**
 * Wraps `GateCard` with the approve/deny mutation every gate surface needs
 * (`POST /api/approvals/{id}`). SECURITY-CRITICAL: `args` passes straight
 * through to `GateCard` untouched — this component adds no formatting of
 * its own.
 */
export default function ApprovalGate({
  approvalId,
  tool,
  args,
  agentId,
  toolClass,
  eyebrow,
  onDecided,
  onDecideLater,
  className,
}: ApprovalGateProps) {
  const [collapsed, setCollapsed] = useState(false)

  const decide = useMutation({
    mutationFn: (approve: boolean) => api.post<ApprovalDecideResponse>(`/api/approvals/${approvalId}`, { approve }),
    onSuccess: (data, approve) => {
      onDecided({ approve, resumed: data.resumed, message: decisionMessage(approve, data.resumed) })
    },
  })

  if (collapsed) {
    return (
      <button type="button" className={styles.reopen} onClick={() => setCollapsed(false)}>
        Decision needed — show
      </button>
    )
  }

  return (
    <GateCard
      tool={tool}
      args={args}
      agentId={agentId}
      toolClass={toolClass}
      eyebrow={eyebrow}
      onApprove={() => decide.mutate(true)}
      onDeny={() => decide.mutate(false)}
      onDecideLater={onDecideLater ?? (() => setCollapsed(true))}
      busy={decide.isPending}
      className={className}
    />
  )
}

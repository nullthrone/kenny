import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api } from '../../api/client'
import type { FleetAgent } from '../../api/types'
import type { Ticket } from './types'
import { blockLabel, transitionLabel } from './stateLabels'
import styles from './TicketActions.module.css'

export interface TicketActionsProps {
  ticket: Ticket
  /** Reassign/assign/note are operator+ routes (`min_role="operator"` server-side) — hidden for a scoped `user`, same UI-convenience-hiding pattern the rest of the console already uses; the server is still the real gate. */
  isOperator: boolean
  meUserId: number | null
  fleetAgents: FleetAgent[]
  onMutated: () => void
}

/**
 * Every lifecycle action but note/chat/inline-edit (their own components):
 * transition, block, unblock, close, reassign, assign. Which transition/block/
 * unblock buttons render is decided ENTIRELY by `ticket.allowed_transitions`,
 * `ticket.allowed_blocks` and `ticket.can_unblock` — nothing here infers legality
 * from `state`. The server computes all three per principal in
 * `webui/tickets.py::_affordances`, so an option only ever appears when the API
 * would actually accept it from the account looking at it.
 *
 * Blocking is what puts a ticket into the Inbox's WAITING group. Without these
 * buttons the group had no entrance from the console at all — a ticket could be
 * unblocked here but only ever blocked from elsewhere.
 */
export default function TicketActions({ ticket, isOperator, meUserId, fleetAgents, onMutated }: TicketActionsProps) {
  const [reassignTarget, setReassignTarget] = useState('')

  const transition = useMutation({
    mutationFn: (to: string) => api.post(`/api/tickets/${ticket.id}/transition`, { to, reason: '' }),
    onSuccess: onMutated,
  })
  const close = useMutation({
    mutationFn: () => api.post(`/api/tickets/${ticket.id}/close`, {}),
    onSuccess: onMutated,
  })
  const unblock = useMutation({
    mutationFn: () => api.post(`/api/tickets/${ticket.id}/unblock`, {}),
    onSuccess: onMutated,
  })
  const block = useMutation({
    mutationFn: (blockedOn: string) => api.post(`/api/tickets/${ticket.id}/block`, { blocked_on: blockedOn }),
    onSuccess: onMutated,
  })
  const reassign = useMutation({
    mutationFn: (agentId: string) => api.post(`/api/tickets/${ticket.id}/reassign`, { agent_id: agentId }),
    onSuccess: () => {
      setReassignTarget('')
      onMutated()
    },
  })
  const assign = useMutation({
    mutationFn: (assigneeUserId: number | null) =>
      api.post(`/api/tickets/${ticket.id}/assign`, { assignee_user_id: assigneeUserId }),
    onSuccess: onMutated,
  })

  const busy =
    transition.isPending || close.isPending || unblock.isPending || block.isPending || reassign.isPending || assign.isPending
  const errors = [transition, close, unblock, block, reassign, assign]
    .map((m) => m.error)
    .filter((e): e is Error => e instanceof Error)

  const isClaimedByMe = meUserId !== null && ticket.assignee_user_id === meUserId
  const isClaimedByOther = ticket.assignee_user_id !== null && ticket.assignee_user_id !== meUserId

  if (
    ticket.allowed_transitions.length === 0 &&
    ticket.allowed_blocks.length === 0 &&
    !ticket.can_unblock &&
    !isOperator
  )
    return null

  return (
    <div className={styles.wrap}>
      <div className={`${styles.row} kc-actions`}>
        {ticket.allowed_transitions.map((state) => (
          <button
            key={state}
            type="button"
            className={`${styles.btn} kc-btn`}
            disabled={busy}
            onClick={() => (state === 'closed' ? close.mutate() : transition.mutate(state))}
          >
            {transitionLabel(state)}
          </button>
        ))}
        {ticket.allowed_blocks.map((reason) => (
          <button
            key={`block-${reason}`}
            type="button"
            className={`${styles.btn} kc-btn`}
            disabled={busy}
            onClick={() => block.mutate(reason)}
          >
            {blockLabel(reason)}
          </button>
        ))}
        {ticket.can_unblock && (
          <button type="button" className={`${styles.btn} kc-btn`} disabled={busy} onClick={() => unblock.mutate()}>
            UNBLOCK
          </button>
        )}
        {isOperator && (
          <button
            type="button"
            className={`${styles.btn} kc-btn`}
            disabled={busy || isClaimedByOther}
            onClick={() => assign.mutate(isClaimedByMe ? null : (meUserId ?? null))}
          >
            {isClaimedByMe ? 'UNCLAIM' : isClaimedByOther ? 'CLAIMED' : 'CLAIM'}
          </button>
        )}
      </div>

      {isOperator && (
        <div className={`${styles.reassignGroup} kc-actions`}>
          <select
            className={styles.select}
            value={reassignTarget}
            disabled={busy}
            onChange={(e) => setReassignTarget(e.target.value)}
            aria-label="Reassign to host"
          >
            <option value="">Reassign to…</option>
            {fleetAgents.map((agent) => (
              <option key={agent.agent_id} value={agent.agent_id} disabled={agent.agent_id === ticket.agent_id}>
                {agent.agent_id}
              </option>
            ))}
          </select>
          <button
            type="button"
            className={`${styles.btn} kc-btn`}
            disabled={busy || !reassignTarget}
            onClick={() => reassign.mutate(reassignTarget)}
          >
            REASSIGN
          </button>
        </div>
      )}

      {errors.length > 0 && <div className={styles.error}>{errors[errors.length - 1].message}</div>}
    </div>
  )
}

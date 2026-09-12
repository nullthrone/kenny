import { useMutation } from '@tanstack/react-query'
import { api } from '../../api/client'
import type { Ticket } from './types'
import { transitionLabel, unblockLabel } from './stateLabels'
import styles from './TicketActions.module.css'

export interface TicketActionsProps {
  ticket: Ticket
  onMutated: () => void
}

/**
 * The one forward move on a ticket, plus whatever the ticket's own situation
 * asks of a person right now. Which of them may render is decided ENTIRELY by
 * `ticket.allowed_transitions` and `ticket.can_unblock` — nothing here infers
 * legality from `state`. The server computes both per principal in
 * `webui/tickets.py::_affordances`, so an option only ever appears when the API
 * would actually accept it from the account looking at it.
 *
 * Three things this surface deliberately does not offer, because the server no
 * longer accepts them from a person either (ADR-0051):
 *
 * - **Setting a block.** A block says something is being waited *for*, and is
 *   written where that wait begins, carrying the `ref` that identifies it. Set
 *   by hand it produced a wait with no referent — most sharply for `approval`,
 *   which three surfaces then described as a pending decision that did not
 *   exist and no clock could ever clear.
 * - **Claiming.** There is no handover of responsibility to model; the trail
 *   already records who did what, when.
 * - **Reassigning to another host.** A ticket is about one machine, fixed when
 *   it is opened.
 */
export default function TicketActions({ ticket, onMutated }: TicketActionsProps) {
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

  const busy = transition.isPending || close.isPending || unblock.isPending
  const errors = [transition, close, unblock]
    .map((m) => m.error)
    .filter((e): e is Error => e instanceof Error)

  function move(to: string) {
    // `closed` has its own route because closing settles the ticket's record
    // (resolution, closed_at) rather than only moving it.
    return to === 'closed' ? close.mutate() : transition.mutate(to)
  }

  // The single move that carries the ticket onward from where it is. Everything
  // else on this row is a correction or an exit, and is styled as one.
  const FORWARD: Record<string, string> = {
    new: 'in_progress',
    in_progress: 'resolved',
    resolved: 'closed',
  }
  const forward = FORWARD[ticket.state]
  const allowed = ticket.allowed_transitions
  const primary = forward && allowed.includes(forward) ? forward : null
  const secondary = allowed.filter((s) => s !== primary && s !== 'cancelled')
  const canCancel = allowed.includes('cancelled')

  if (!primary && secondary.length === 0 && !canCancel && !ticket.can_unblock) return null

  return (
    <div className={styles.wrap}>
      <div className={`${styles.row} kc-actions`}>
        {primary && (
          <button
            type="button"
            className={`${styles.btn} ${styles.primary} kc-btn`}
            disabled={busy}
            onClick={() => move(primary)}
          >
            {transitionLabel(ticket.state, primary)}
          </button>
        )}

        {ticket.can_unblock && (
          <button
            type="button"
            className={`${styles.btn} ${styles.resume} kc-btn`}
            disabled={busy}
            onClick={() => unblock.mutate()}
          >
            {unblockLabel(ticket.blocked_on)}
          </button>
        )}

        {secondary.map((state) => (
          <button
            key={state}
            type="button"
            className={`${styles.btn} kc-btn`}
            disabled={busy}
            onClick={() => move(state)}
          >
            {transitionLabel(ticket.state, state)}
          </button>
        ))}

        {canCancel && (
          <button
            type="button"
            className={`${styles.btn} ${styles.danger} kc-btn`}
            disabled={busy}
            onClick={() => transition.mutate('cancelled')}
          >
            {transitionLabel(ticket.state, 'cancelled')}
          </button>
        )}
      </div>

      {errors.length > 0 && <div className={styles.error}>{errors[errors.length - 1].message}</div>}
    </div>
  )
}

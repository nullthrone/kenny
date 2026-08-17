import type { Ticket } from './types'

export interface StatusChip {
  label: string
  color: string
}

/**
 * The header status chip. `blocked_on` wins over `state` when both are
 * informative — an `in_progress` ticket that's actually sitting on a gate
 * reads as "AWAITING APPROVAL", matching the design's ticket #41 example,
 * not the less useful "IN PROGRESS".
 */
export function ticketStatusChip(ticket: Ticket): StatusChip {
  if (ticket.blocked_on === 'approval') return { label: 'AWAITING APPROVAL', color: 'var(--brass-600)' }
  if (ticket.blocked_on === 'operator') return { label: 'NEEDS OPERATOR', color: 'var(--warn)' }
  if (ticket.blocked_on === 'user') return { label: 'WAITING ON REQUESTER', color: 'var(--warn)' }
  switch (ticket.state) {
    case 'new':
      return { label: 'NEW', color: 'var(--text-muted)' }
    case 'in_progress':
      return { label: 'IN PROGRESS', color: 'var(--text-muted)' }
    case 'resolved':
      return { label: 'RESOLVED', color: 'var(--ok)' }
    case 'closed':
      return { label: 'CLOSED', color: 'var(--text-faint)' }
    case 'cancelled':
      return { label: 'CANCELLED', color: 'var(--text-faint)' }
    default:
      return { label: ticket.state.toUpperCase(), color: 'var(--text-muted)' }
  }
}

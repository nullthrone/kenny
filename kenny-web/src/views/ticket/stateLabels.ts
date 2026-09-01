/**
 * Display-only labels for a lifecycle state. Which states may even be
 * offered as buttons comes entirely from `Ticket.allowed_transitions` —
 * this only decides what word appears on the button once the server has
 * already licensed it.
 */
const TRANSITION_VERBS: Record<string, string> = {
  new: 'REOPEN',
  in_progress: 'START WORK',
  resolved: 'MARK RESOLVED',
  closed: 'CLOSE TICKET',
  cancelled: 'CANCEL TICKET',
}

export function transitionLabel(state: string): string {
  return TRANSITION_VERBS[state] ?? state.toUpperCase().replace(/_/g, ' ')
}

/**
 * Display-only labels for a block reason (`tickets.py`'s `BLOCKED_REASONS`).
 * As with transitions, which of them may be offered comes entirely from
 * `Ticket.allowed_blocks`; this only names the button.
 *
 * Each label says who the ticket ends up waiting on, because that is what the
 * Inbox's WAITING group then groups it by — "WAIT ON REQUESTER" reads as the
 * consequence, "user" would only read as a category.
 */
const BLOCK_VERBS: Record<string, string> = {
  user: 'WAIT ON REQUESTER',
  operator: 'WAIT ON OPERATOR',
  approval: 'WAIT ON APPROVAL',
}

export function blockLabel(reason: string): string {
  return BLOCK_VERBS[reason] ?? `WAIT ON ${reason.toUpperCase().replace(/_/g, ' ')}`
}

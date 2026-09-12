/**
 * Display-only labels for a lifecycle move. Which moves may even be offered
 * comes entirely from `Ticket.allowed_transitions` — this only decides what
 * word appears on the button once the server has already licensed it.
 *
 * The label needs the state being left, not just the one being entered:
 * `resolved -> in_progress` and `new -> in_progress` share a destination and
 * mean opposite things. Keying on the destination alone is what had a resolved
 * ticket's reopen button reading "START WORK".
 */
const TRANSITION_VERBS: Record<string, string> = {
  'new>in_progress': 'START WORK',
  'resolved>in_progress': 'REOPEN',
  resolved: 'MARK RESOLVED',
  closed: 'CLOSE NOW',
  cancelled: 'CANCEL TICKET',
}

export function transitionLabel(from: string, to: string): string {
  return (
    TRANSITION_VERBS[`${from}>${to}`] ??
    TRANSITION_VERBS[to] ??
    to.toUpperCase().replace(/_/g, ' ')
  )
}

/**
 * The label for clearing a block, which is the operator's half of a
 * conversation the machine started: kenny asked the requester something, or
 * the stall sweep handed the ticket to a human. Each label says what the click
 * asserts, because that is what the reader has to be sure of before clicking —
 * "UNBLOCK" named the column, not the claim.
 *
 * There is deliberately no entry for `approval`: a ticket waiting on a gate
 * leaves that state by the gate being answered or expiring, never by someone
 * declaring the wait over. The server agrees — `_UNBLOCK_CLEARERS["approval"]`
 * is `{"system"}` — so `can_unblock` is false there and this is never asked.
 */
const UNBLOCK_VERBS: Record<string, string> = {
  user: 'GOT AN ANSWER',
  operator: 'PICK THIS UP',
}

export function unblockLabel(blockedOn: string): string {
  return UNBLOCK_VERBS[blockedOn] ?? 'RESUME'
}

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

export function stateDisplay(state: string): string {
  return state.toUpperCase().replace(/_/g, ' ')
}

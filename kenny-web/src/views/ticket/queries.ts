/**
 * Shared React Query key builders for the ticket surfaces. `InboxTicket`,
 * `TicketChat` and `ApprovalGate`'s callers all need to invalidate/read the
 * exact same cache entries — a typo'd key in one file would silently stop
 * sharing the cache instead of erroring, so this is the one place they're
 * spelled.
 */
export const ticketKey = (id: string) => ['ticket', id] as const
export const ticketEventsKey = (id: string) => ['ticket', id, 'events'] as const
export const ticketApprovalKey = (id: string) => ['approvals', id] as const

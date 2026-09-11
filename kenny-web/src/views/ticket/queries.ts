/**
 * Shared React Query key builders for the ticket surfaces. `InboxTicket`,
 * `ApprovalGate`'s callers and the ticket-bound chat drawer all need to
 * invalidate/read the exact same cache entries — a typo'd key in one file
 * would silently stop sharing the cache instead of erroring, so this is the
 * one place they're spelled.
 *
 * `timeline` and `events` are two readings of the same rows and are
 * invalidated together: a turn that writes to the trail moves both.
 */
export const ticketKey = (id: string) => ['ticket', id] as const
export const ticketEventsKey = (id: string) => ['ticket', id, 'events'] as const
export const ticketTimelineKey = (id: string) => ['ticket', id, 'timeline'] as const
export const ticketApprovalKey = (id: string) => ['approvals', id] as const
export const ticketAlertsKey = (id: string) => ['ticket', id, 'alerts'] as const

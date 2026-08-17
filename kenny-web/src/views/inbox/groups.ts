import type { InboxGroup } from '../../api/types'

/** Chip order, matching the prototype's `inboxGroupsDef` and `TicketStore.counts`'s bucket order. */
export const INBOX_GROUPS: ReadonlyArray<{ key: InboxGroup; label: string }> = [
  { key: 'needs_you', label: 'NEEDS YOU' },
  { key: 'waiting', label: 'WAITING' },
  { key: 'working', label: 'WORKING' },
  { key: 'new', label: 'NEW' },
  { key: 'done', label: 'DONE' },
]

export const DEFAULT_INBOX_GROUP: InboxGroup = 'needs_you'

export function isInboxGroup(value: string | undefined): value is InboxGroup {
  return INBOX_GROUPS.some((g) => g.key === value)
}

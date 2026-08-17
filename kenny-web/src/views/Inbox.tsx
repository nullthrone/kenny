import { useState } from 'react'
import { useNavigate, useParams } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { InboxResponse } from '../api/types'
import Chip from '../components/Chip/Chip'
import EmptyState from '../components/EmptyState/EmptyState'
import { Inbox as InboxIcon } from '../components/icons'
import InboxRow from './inbox/InboxRow'
import NewTicketModal from './inbox/NewTicketModal'
import { DEFAULT_INBOX_GROUP, INBOX_GROUPS, isInboxGroup } from './inbox/groups'
import type { Ticket, TicketVocabulary } from './ticket/types'
import type { DecisionOutcome } from './ticket/ApprovalGate'
import styles from './inbox/Inbox.module.css'

/**
 * `#/inbox`, `#/inbox/:group` — the merged queue: approvals, flagged
 * sections, and tickets, ranked by who it waits on. Every row's shape
 * (kind, gate, legality) is entirely server-computed (`GET /api/inbox`) —
 * this view renders it, it does not re-derive it.
 */
export default function Inbox() {
  const { group: groupParam } = useParams<{ group?: string }>()
  const group = isInboxGroup(groupParam) ? groupParam : DEFAULT_INBOX_GROUP
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [newTicketOpen, setNewTicketOpen] = useState(false)
  const [banner, setBanner] = useState<{ text: string; warn: boolean } | null>(null)

  const inbox = useQuery({
    queryKey: ['inbox', group],
    queryFn: () => api.get<InboxResponse>(`/api/inbox?group=${encodeURIComponent(group)}`),
  })

  // Server-authoritative lifecycle vocabulary — no client-side legality
  // logic reads it here, but priming the cache now (long staleTime, shared
  // query key) means the ticket detail view opened from a row below never
  // waits on it separately, matching "cached for the session".
  useQuery({
    queryKey: ['tickets', 'vocabulary'],
    queryFn: () => api.get<TicketVocabulary>('/api/tickets/vocabulary'),
    staleTime: Infinity,
  })

  function handleDecided(_item: unknown, outcome: DecisionOutcome) {
    setBanner({ text: outcome.message, warn: !outcome.resumed })
    void queryClient.invalidateQueries({ queryKey: ['inbox'] })
  }

  function handleCreated(ticket: Ticket) {
    setNewTicketOpen(false)
    void queryClient.invalidateQueries({ queryKey: ['inbox'] })
    navigate(`/inbox/ticket/${ticket.id}`)
  }

  const counts = inbox.data?.counts

  return (
    <div className={`${styles.root} kc-content kc-view`}>
      <div className={styles.headerRow}>
        <h1 className={`kc-h1 ${styles.title}`}>Inbox</h1>
        <button type="button" className={`${styles.newTicket} kc-btn`} onClick={() => setNewTicketOpen(true)}>
          NEW TICKET
        </button>
      </div>
      <p className={styles.lede}>Approvals, flagged sections, and tickets — one queue, ranked by who it waits on.</p>

      {banner && (
        <div className={`${styles.banner}${banner.warn ? ` ${styles.bannerWarn}` : ''}`}>
          <span>{banner.text}</span>
          <button type="button" className={styles.bannerDismiss} onClick={() => setBanner(null)}>
            DISMISS
          </button>
        </div>
      )}

      <div className={styles.groups}>
        {INBOX_GROUPS.map((g) => (
          <Chip
            key={g.key}
            label={g.label}
            count={counts ? counts[g.key] : '—'}
            active={g.key === group}
            onClick={() => navigate(g.key === DEFAULT_INBOX_GROUP ? '/inbox' : `/inbox/${g.key}`)}
          />
        ))}
      </div>

      {inbox.isError && (
        <EmptyState
          icon={InboxIcon}
          title="Could not load the inbox"
          message={(inbox.error as Error).message}
          action={{ label: 'RETRY', onClick: () => void inbox.refetch() }}
        />
      )}

      {inbox.data && inbox.data.items.length === 0 && (
        <EmptyState icon={InboxIcon} title="All quiet" message="Nothing in this group waits on anyone right now." />
      )}

      {inbox.data && inbox.data.items.length > 0 && (
        <div className={styles.list}>
          {inbox.data.items.map((item) => (
            <InboxRow key={item.id} item={item} onDecided={handleDecided} />
          ))}
        </div>
      )}

      <NewTicketModal open={newTicketOpen} onClose={() => setNewTicketOpen(false)} onCreated={handleCreated} />
    </div>
  )
}

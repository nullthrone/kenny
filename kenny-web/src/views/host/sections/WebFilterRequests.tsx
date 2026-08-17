import { Link } from 'react-router'
import type { WebfilterBypassRequest } from '../types'
import { useWebfilterRequests } from '../api'
import { formatRelativeTime } from '../format'
import styles from './WebFilterRequests.module.css'

export interface WebFilterRequestsProps {
  agentId: string
  /** Prefills the custom-domain form above with a requested domain, action
   * "allow" — granting one IS that form's existing allow-domain action
   * (ADR-0055); this view never calls a second approval endpoint. */
  onRequestDomain: (domain: string) => void
}

function RequestRow({ request, onRequestDomain }: { request: WebfilterBypassRequest; onRequestDomain: (domain: string) => void }) {
  const { ticket, requested_domains: domains } = request
  return (
    <div className={styles.row}>
      <div className={styles.meta}>
        <Link to={`/inbox/ticket/${ticket.id}`} className={styles.title}>
          #{ticket.number} {ticket.title}
        </Link>
        <span className={styles.age}>{formatRelativeTime(ticket.created_at)}</span>
      </div>
      {ticket.summary && <p className={styles.summary}>{ticket.summary}</p>}
      {domains.length > 0 && (
        <div className={styles.domains}>
          {domains.map((d) => (
            <button key={d} type="button" className={styles.domainChip} onClick={() => onRequestDomain(d)}>
              {d}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * Pending `web_filter`-category tickets for this host. Each row links to its
 * real ticket in the Inbox rather than offering its own approve/deny —
 * there is no second lifecycle here. Clicking a requested domain only
 * prefills the custom-domain form below with that domain and "allow" so
 * granting stays the one ordinary action this modal already has.
 */
export default function WebFilterRequests({ agentId, onRequestDomain }: WebFilterRequestsProps) {
  const requests = useWebfilterRequests(agentId, true)
  const rows = requests.data?.requests ?? []

  return (
    <div>
      {requests.isPending ? (
        <p className={styles.empty}>Loading bypass requests…</p>
      ) : requests.isError ? (
        <p className={styles.empty}>Could not load bypass requests.</p>
      ) : rows.length === 0 ? (
        <p className={styles.empty}>No open bypass requests.</p>
      ) : (
        <div className={styles.list}>
          {rows.map((r) => (
            <RequestRow key={r.ticket.id} request={r} onRequestDomain={onRequestDomain} />
          ))}
        </div>
      )}
    </div>
  )
}

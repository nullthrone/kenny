import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import type { FleetResponse } from '../../api/types'
import TicketDraftForm, {
  type TicketDraftValue,
} from '../TicketDraftForm/TicketDraftForm'
import styles from './TicketDraftCard.module.css'

export interface TicketDraftCardProps {
  itemId: string
  title: string
  summary: string
  agentId: string
  resolution: 'pending' | 'created' | 'dismissed'
  ticketId?: string
  ticketNumber?: number
  onCreate: (
    itemId: string,
    draft: { title: string; summary: string; agentId: string | null; startImmediately: boolean },
  ) => Promise<void>
  onDismiss: (itemId: string) => void
}

/**
 * The ticket kenny proposed, as a form the operator finishes.
 *
 * Nothing has been created when this renders. The card is the only thing that
 * can create anything, and it does it through `POST /api/tickets` like the
 * inbox's own modal — so the ticket is opened by the person whose name goes on
 * it, carrying the wording they left in the fields rather than the wording the
 * model put there.
 *
 * It stays in the transcript once answered, the way a decided gate does: the
 * conversation should still say a ticket came out of it.
 */
export default function TicketDraftCard({
  itemId,
  title,
  summary,
  agentId,
  resolution,
  ticketId,
  ticketNumber,
  onCreate,
  onDismiss,
}: TicketDraftCardProps) {
  const [value, setValue] = useState<TicketDraftValue>({
    title,
    description: summary,
    host: agentId || null,
    startImmediately: true,
  })
  const [hosts, setHosts] = useState<string[]>(agentId ? [agentId] : [])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Fetched with the plain client rather than a query: the drawer is mounted
  // by Shell and stays renderable without a query client (see
  // `chat/types.ts`'s TicketChatTarget). A failure is not an error state —
  // the drafted host is already a pill, so the form still works.
  useEffect(() => {
    if (resolution !== 'pending') return
    let live = true
    api
      .get<FleetResponse>('/api/fleet')
      .then((fleet) => {
        if (live) setHosts(fleet.agents.map((a) => a.agent_id))
      })
      .catch(() => undefined)
    return () => {
      live = false
    }
  }, [resolution])

  if (resolution === 'created') {
    return (
      <div className={styles.done}>
        Opened{' '}
        <a className={styles.link} href={`#/inbox/ticket/${encodeURIComponent(ticketId ?? '')}`}>
          ticket #{ticketNumber}
        </a>{' '}
        — {value.title}
      </div>
    )
  }

  if (resolution === 'dismissed') {
    return <div className={styles.done}>Draft discarded — {title}</div>
  }

  const canCreate = value.title.trim().length > 0 && value.description.trim().length > 0 && !busy

  async function handleCreate() {
    setBusy(true)
    setError(null)
    try {
      await onCreate(itemId, {
        title: value.title,
        summary: value.description,
        agentId: value.host,
        startImmediately: value.startImmediately,
      })
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={styles.card}>
      <p className={styles.heading}>DRAFT TICKET — nothing is filed until you open it</p>
      <TicketDraftForm
        idPrefix={`draft-${itemId}`}
        hosts={hosts}
        value={value}
        onChange={setValue}
        showTitle
        disabled={busy}
      />
      {error && <p className={styles.error}>{error}</p>}
      <div className={`${styles.footer} kc-actions`}>
        <button
          type="button"
          className={`${styles.dismiss} kc-btn`}
          disabled={busy}
          onClick={() => onDismiss(itemId)}
        >
          DISCARD
        </button>
        <button
          type="button"
          className={`${styles.create} kc-btn`}
          disabled={!canCreate}
          onClick={handleCreate}
        >
          {busy ? 'OPENING…' : 'OPEN TICKET'}
        </button>
      </div>
    </div>
  )
}

import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import styles from './TicketDraftCard.module.css'
import own from './AgentRunProposalCard.module.css'

export interface AgentRunProposalCardProps {
  itemId: string
  agentId: string
  /** '' for an agent that is not bound to a PC. */
  hostId: string
  reason: string
  resolution: 'pending' | 'started' | 'dismissed'
  runId?: string
  /** Starts the preview. Rejects with the server's text when it refuses (400/409/503). */
  onStart: (itemId: string, agentId: string, hostId: string) => Promise<void>
  onDismiss: (itemId: string) => void
}

/**
 * The preview run kenny proposed, as a card the operator accepts or puts away.
 *
 * Nothing has started when this renders. The card is the only thing that can
 * start anything, and it does it through the same
 * `POST /api/specialized-agents/{id}/runs` the Specialized agents view uses —
 * so the run is started by the person who pressed the button (ADR-0063: the
 * copilot proposes, a person acts). A preview is always shadow: it changes
 * nothing on any PC.
 *
 * It stays in the transcript once answered, the way a draft ticket does.
 */
export default function AgentRunProposalCard({
  itemId,
  agentId,
  hostId,
  reason,
  resolution,
  runId,
  onStart,
  onDismiss,
}: AgentRunProposalCardProps) {
  const [title, setTitle] = useState(agentId)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Fetched with the plain client, like the ticket draft's host list: the
  // drawer stays renderable without a query client. A failure only costs the
  // friendly name — the id is already on the card.
  useEffect(() => {
    let live = true
    api
      .get<{ agents: { id: string; title: string }[] }>('/api/specialized-agents')
      .then((res) => {
        const found = res.agents.find((a) => a.id === agentId)
        if (live && found?.title) setTitle(found.title)
      })
      .catch(() => undefined)
    return () => {
      live = false
    }
  }, [agentId])

  const agentLink = `#/admin/agents/${encodeURIComponent(agentId)}`

  if (resolution === 'started') {
    return (
      <div className={styles.done}>
        Preview started — run <code>{runId}</code> of{' '}
        <a className={styles.link} href={agentLink}>
          {title}
        </a>
        {hostId ? ` on ${hostId}` : ''}
      </div>
    )
  }

  if (resolution === 'dismissed') {
    return <div className={styles.done}>Preview not started — {title}</div>
  }

  async function handleStart() {
    setBusy(true)
    setError(null)
    try {
      await onStart(itemId, agentId, hostId)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={styles.card}>
      <p className={styles.heading}>PROPOSED PREVIEW — nothing starts until you press the button</p>
      <dl className={own.facts}>
        <dt>AGENT</dt>
        <dd>{title}</dd>
        <dt>PC</dt>
        <dd>{hostId || 'none — this agent is not bound to a PC'}</dd>
        <dt>WHY</dt>
        <dd>{reason}</dd>
      </dl>
      <p className={own.note}>
        A preview runs the agent once in shadow: it reads, decides and reports. A preview never changes anything on
        any PC; every change it would make is only recorded as a recommendation. It does spend model tokens.
      </p>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
      <div className={`${styles.footer} kc-actions`}>
        <button
          type="button"
          className={`${styles.dismiss} kc-btn`}
          disabled={busy}
          onClick={() => onDismiss(itemId)}
        >
          DISMISS
        </button>
        <button type="button" className={`${styles.create} kc-btn`} disabled={busy} onClick={handleStart}>
          {busy ? 'STARTING…' : 'START PREVIEW'}
        </button>
      </div>
    </div>
  )
}

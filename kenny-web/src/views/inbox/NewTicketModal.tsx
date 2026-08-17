import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import type { FleetResponse } from '../../api/types'
import Modal from '../../components/Modal/Modal'
import { X, ICON_STROKE_WIDTH } from '../../components/icons'
import type { Ticket } from '../ticket/types'
import styles from './NewTicketModal.module.css'

export interface NewTicketModalProps {
  open: boolean
  onClose: () => void
  onCreated: (ticket: Ticket) => void
}

const TITLE_MAX = 80

/** The description's first line, capped — `POST /api/tickets` requires a `title` the design's textarea-only form doesn't collect. */
function deriveTitle(description: string): string {
  const firstLine = description.trim().split('\n', 1)[0] ?? ''
  return firstLine.length > TITLE_MAX ? `${firstLine.slice(0, TITLE_MAX - 1)}…` : firstLine
}

/**
 * "NEW TICKET" modal: host picker, description, "start working
 * immediately" checkbox. `agent_id: null` (the "no PC yet" pill) is kept —
 * the old dashboard allowed triage tickets with no target host, and
 * nothing here should drop that.
 */
export default function NewTicketModal({ open, onClose, onCreated }: NewTicketModalProps) {
  const [selectedHost, setSelectedHost] = useState<string | null | undefined>(undefined)
  const [description, setDescription] = useState('')
  const [startImmediately, setStartImmediately] = useState(true)

  const fleet = useQuery({
    queryKey: ['fleet'],
    queryFn: () => api.get<FleetResponse>('/api/fleet'),
    enabled: open,
  })

  const host = selectedHost === undefined ? (fleet.data?.agents[0]?.agent_id ?? null) : selectedHost

  const create = useMutation({
    mutationFn: () =>
      api.post<Ticket>('/api/tickets', {
        title: deriveTitle(description) || 'New ticket',
        summary: description.trim(),
        agent_id: host,
        origin: 'dashboard',
        start_immediately: startImmediately,
      }),
    onSuccess: (ticket) => {
      setSelectedHost(undefined)
      setDescription('')
      setStartImmediately(true)
      onCreated(ticket)
    },
  })

  function handleClose() {
    create.reset()
    onClose()
  }

  const canCreate = description.trim().length > 0 && !create.isPending

  return (
    <Modal open={open} onClose={handleClose} labelledBy="new-ticket-title" width={520}>
      <div className={styles.header}>
        <span id="new-ticket-title" className={styles.headerTitle}>
          NEW TICKET
        </span>
        <button type="button" onClick={handleClose} className={styles.close} aria-label="Close">
          <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
        </button>
      </div>
      <div className={styles.body}>
        <label className={styles.label} htmlFor="new-ticket-host-group">
          Which PC?
        </label>
        <div className={styles.hosts} id="new-ticket-host-group" role="group" aria-label="Which PC?">
          {(fleet.data?.agents ?? []).map((agent) => (
            <button
              key={agent.agent_id}
              type="button"
              aria-pressed={host === agent.agent_id}
              className={`${styles.hostPill} kc-btn${host === agent.agent_id ? ` ${styles.hostPillActive}` : ''}`}
              onClick={() => setSelectedHost(agent.agent_id)}
            >
              {agent.agent_id}
            </button>
          ))}
          <button
            type="button"
            aria-pressed={host === null}
            className={`${styles.hostPill} kc-btn${host === null ? ` ${styles.hostPillActive}` : ''}`}
            onClick={() => setSelectedHost(null)}
          >
            No PC yet
          </button>
        </div>

        <label className={styles.label} htmlFor="new-ticket-description">
          What should kenny do?
        </label>
        <textarea
          id="new-ticket-description"
          className={styles.textarea}
          placeholder="Describe the problem or task — kenny plans the steps and asks before changing anything."
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
        <label className={styles.checkboxRow}>
          <input
            type="checkbox"
            className={styles.checkbox}
            checked={startImmediately}
            onChange={(e) => setStartImmediately(e.target.checked)}
          />
          Start working immediately (read-only steps only until I approve)
        </label>

        {create.isError && <p className={styles.error}>{(create.error as Error).message}</p>}

        <div className={`${styles.footer} kc-actions`}>
          <button type="button" onClick={handleClose} className={`${styles.cancel} kc-btn`}>
            CANCEL
          </button>
          <button
            type="button"
            onClick={() => create.mutate()}
            disabled={!canCreate}
            className={`${styles.create} kc-btn`}
          >
            {create.isPending ? 'CREATING…' : 'CREATE TICKET'}
          </button>
        </div>
      </div>
    </Modal>
  )
}

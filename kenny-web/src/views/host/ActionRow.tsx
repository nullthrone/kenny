import { useState } from 'react'
import { useNavigate } from 'react-router'
import Modal from '../../components/Modal/Modal'
import { RefreshCw, LifeBuoy, ArrowUpCircle, Trash2, X, ICON_STROKE_WIDTH } from '../../components/icons'
import { useRefreshAgent, useRemoteHelp, useUpdateAgent, useRemoveAgent, useSetChannel } from './api'
import styles from './ActionRow.module.css'

export interface ActionRowProps {
  agentId: string
  channel?: string
}

/**
 * The host action button row. The prototype's demo data shows six buttons
 * (REFRESH, REMOTE HELP, REINSTALL, RE-SHARE, UPDATE AGENT, REMOVE) — only
 * four have an endpoint assigned to Host in notes/view-endpoint-map.md.
 * REINSTALL/RE-SHARE's underlying routes (`/api/agents/{id}/installer`,
 * `/api/agents/{id}/share-link`) are listed only under Fleet's Add-a-PC
 * wizard, so they're left off this row rather than wired to a route outside
 * this view's map.
 */
export default function ActionRow({ agentId, channel }: ActionRowProps) {
  const navigate = useNavigate()
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null)
  const [confirmRemove, setConfirmRemove] = useState(false)

  const refresh = useRefreshAgent(agentId)
  const remoteHelp = useRemoteHelp(agentId)
  const update = useUpdateAgent(agentId)
  const remove = useRemoveAgent()
  const setChannel = useSetChannel(agentId)

  function doRefresh() {
    setMessage(null)
    refresh.mutate(undefined, {
      onSuccess: (r) => setMessage({ text: r.warning ?? 'Refreshed.', error: Boolean(r.warning) }),
      onError: (e) => setMessage({ text: `Could not refresh: ${e instanceof Error ? e.message : 'unknown error'}`, error: true }),
    })
  }

  function doRemoteHelp() {
    setMessage(null)
    remoteHelp.mutate(undefined, {
      onSuccess: (r) =>
        setMessage({
          text: r.note || 'Quick Assist was launched on the desktop. A human helper still shares the code.',
          error: false,
        }),
      onError: (e) => setMessage({ text: `Could not start remote help: ${e instanceof Error ? e.message : 'unknown error'}`, error: true }),
    })
  }

  function doUpdate() {
    setMessage(null)
    update.mutate(undefined, {
      onSuccess: (r) => setMessage({ text: r.version ? `Update to ${r.version} pushed.` : 'Update pushed.', error: false }),
      onError: (e) => setMessage({ text: `Could not push an update: ${e instanceof Error ? e.message : 'unknown error'}`, error: true }),
    })
  }

  function doRemove() {
    remove.mutate(agentId, {
      onSuccess: () => navigate('/fleet'),
      onError: (e) => setMessage({ text: `Could not remove this host: ${e instanceof Error ? e.message : 'unknown error'}`, error: true }),
    })
  }

  return (
    <div>
      <div className={`${styles.row} kc-hostactions`}>
        <button type="button" className={styles.button} onClick={doRefresh} disabled={refresh.isPending}>
          <RefreshCw width={13} height={13} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          {refresh.isPending ? 'REFRESHING…' : 'REFRESH'}
        </button>
        <button type="button" className={styles.button} onClick={doRemoteHelp} disabled={remoteHelp.isPending}>
          <LifeBuoy width={13} height={13} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          {remoteHelp.isPending ? 'STARTING…' : 'REMOTE HELP'}
        </button>
        <button type="button" className={styles.button} onClick={doUpdate} disabled={update.isPending}>
          <ArrowUpCircle width={13} height={13} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          {update.isPending ? 'UPDATING…' : 'UPDATE AGENT'}
        </button>
        <button type="button" className={`${styles.button} ${styles.remove}`} onClick={() => setConfirmRemove(true)}>
          <Trash2 width={13} height={13} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          REMOVE
        </button>
      </div>

      {message && <p className={`${styles.message}${message.error ? ` ${styles.messageError}` : ''}`}>{message.text}</p>}

      <div className={styles.channelRow}>
        <span className={styles.channelLabel}>CHANNEL</span>
        {(['stable', 'dev'] as const).map((c) => (
          <button
            key={c}
            type="button"
            className={`${styles.channelButton}${channel === c ? ` ${styles.channelActive}` : ''}`}
            disabled={setChannel.isPending}
            onClick={() => setChannel.mutate(c)}
          >
            {c.toUpperCase()}
          </button>
        ))}
      </div>

      <Modal open={confirmRemove} onClose={() => setConfirmRemove(false)} labelledBy="remove-host-title" width={440}>
        <div className={styles.modalHeader}>
          <span id="remove-host-title" className={styles.modalTitle}>
            REMOVE HOST
          </span>
          <button type="button" className={styles.cancelButton} style={{ minHeight: 44, minWidth: 44, padding: 0, border: 'none' }} onClick={() => setConfirmRemove(false)}>
            <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          </button>
        </div>
        <div className={styles.confirmBody}>
          <p className={styles.confirmText}>
            This purges every record of <strong>{agentId}</strong> — telemetry, screenshots, and history. The agent
            itself keeps running and can be re-added later. This cannot be undone.
          </p>
          <div className={styles.confirmActions}>
            <button type="button" className={styles.cancelButton} onClick={() => setConfirmRemove(false)}>
              CANCEL
            </button>
            <button type="button" className={styles.confirmRemoveButton} onClick={doRemove} disabled={remove.isPending}>
              {remove.isPending ? 'REMOVING…' : 'REMOVE HOST'}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  )
}

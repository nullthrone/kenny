import { useState } from 'react'
import Modal from '../../../components/Modal/Modal'
import { X, ICON_STROKE_WIDTH } from '../../../components/icons'
import type { BackupEntry } from '../types'
import shared from '../shared.module.css'

export interface RestoreConfirmModalProps {
  backup: BackupEntry | null
  onClose: () => void
  onConfirm: () => void
  busy: boolean
  error: string | null
}

/**
 * Restore is the most destructive action in the product: it overwrites the
 * live database and restarts the server. Gated like an approval — an
 * explicit statement of what gets overwritten, plus a typed confirmation —
 * never a plain button.
 */
export default function RestoreConfirmModal({ backup, onClose, onConfirm, busy, error }: RestoreConfirmModalProps) {
  const [typed, setTyped] = useState('')

  function handleClose() {
    setTyped('')
    onClose()
  }

  if (!backup) return null

  const createdLabel = new Date(backup.created_at).toLocaleString()
  const confirmed = typed.trim() === 'RESTORE'

  return (
    <Modal open onClose={handleClose} labelledBy="restore-confirm-title" width={480}>
      <div id="restore-confirm-title" className={shared.cardTitle} style={{ padding: '20px 22px 0', color: 'var(--danger)' }}>
        Restore database
        <button
          type="button"
          onClick={handleClose}
          style={{ float: 'right', background: 'transparent', border: 'none', color: 'var(--text-muted)' }}
          aria-label="Close"
        >
          <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} />
        </button>
      </div>
      <div style={{ padding: '16px 22px' }}>
        <div className={shared.warnBox}>
          This overwrites the live database with <strong className={shared.mono}>{backup.name}</strong>, created {createdLabel}.
          Everything written since that backup — tickets, telemetry, settings changes — is discarded. The server restarts to
          apply it and will be briefly unreachable.
        </div>
        {error && <div className={shared.errorBox}>{error}</div>}
        <label className={shared.field} style={{ marginTop: 4 }}>
          <span className={shared.fieldLabel}>TYPE RESTORE TO CONFIRM</span>
          <input
            type="text"
            className={shared.input}
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            autoComplete="off"
            autoFocus
          />
        </label>
      </div>
      <div className={shared.actions} style={{ padding: '0 22px 20px' }}>
        <button type="button" className={shared.btnDanger} onClick={onConfirm} disabled={!confirmed || busy}>
          {busy ? 'RESTORING…' : 'RESTORE — OVERWRITE LIVE DATABASE'}
        </button>
        <button type="button" className={shared.btn} onClick={handleClose} disabled={busy}>
          CANCEL
        </button>
      </div>
    </Modal>
  )
}

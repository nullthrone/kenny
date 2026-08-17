import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../../api/client'
import Modal from '../../../components/Modal/Modal'
import { X, ICON_STROKE_WIDTH } from '../../../components/icons'
import type { BackupTarget, BackupTargetKind } from '../types'
import shared from '../shared.module.css'

export interface BackupTargetModalProps {
  open: boolean
  onClose: () => void
  /** Present when editing; absent when adding a new target. */
  target?: BackupTarget | null
}

const SECRET_KEYS = ['password', 'private_key', 'token'] as const

function fieldsFor(kind: BackupTargetKind): { key: string; label: string; secret?: boolean }[] {
  switch (kind) {
    case 'http':
      return [
        { key: 'url', label: 'URL' },
        { key: 'token', label: 'Bearer token', secret: true },
      ]
    case 'scp':
      return [
        { key: 'host', label: 'Host' },
        { key: 'port', label: 'Port' },
        { key: 'username', label: 'Username' },
        { key: 'password', label: 'Password', secret: true },
        { key: 'private_key', label: 'Private key', secret: true },
        { key: 'remote_dir', label: 'Remote directory' },
      ]
    case 'ftp':
      return [
        { key: 'host', label: 'Host' },
        { key: 'port', label: 'Port' },
        { key: 'username', label: 'Username' },
        { key: 'password', label: 'Password', secret: true },
        { key: 'remote_dir', label: 'Remote directory' },
        { key: 'use_tls', label: 'Use TLS (true/false)' },
      ]
  }
}

/** Add or edit a `/api/backup-targets` remote push destination (http/scp/ftp). */
export default function BackupTargetModal({ open, onClose, target }: BackupTargetModalProps) {
  const queryClient = useQueryClient()
  const isEdit = !!target
  const [kind, setKind] = useState<BackupTargetKind>(target?.kind ?? 'http')
  const [label, setLabel] = useState(target?.label ?? '')
  const [config, setConfig] = useState<Record<string, string>>(() => {
    const out: Record<string, string> = {}
    if (target) {
      for (const [k, v] of Object.entries(target.config)) {
        if (v !== null && v !== undefined && !k.endsWith('_set')) out[k] = String(v)
      }
    }
    return out
  })

  const save = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = { ...config }
      // A blank secret field means "leave unchanged" on edit, per the API's own merge rule.
      for (const key of SECRET_KEYS) {
        if (isEdit && (!body[key] || body[key] === '')) delete body[key]
      }
      if (isEdit && target) {
        return api.put(`/api/backup-targets/${target.id}`, { label: label.trim(), config: body })
      }
      return api.post('/api/backup-targets', { kind, label: label.trim(), config: body })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'backups'] })
      onClose()
    },
  })

  function setField(key: string, value: string) {
    setConfig((c) => ({ ...c, [key]: value }))
  }

  return (
    <Modal open={open} onClose={onClose} labelledBy="backup-target-title" width={480}>
      <div id="backup-target-title" className={shared.cardTitle} style={{ padding: '20px 22px 0' }}>
        {isEdit ? 'Edit backup target' : 'Add backup target'}
        <button
          type="button"
          onClick={onClose}
          style={{ float: 'right', background: 'transparent', border: 'none', color: 'var(--text-muted)' }}
          aria-label="Close"
        >
          <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} />
        </button>
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          save.mutate()
        }}
      >
        <div style={{ padding: '16px 22px', display: 'flex', flexDirection: 'column', gap: 14, maxHeight: '60vh', overflowY: 'auto' }}>
          {save.isError && (
            <div className={shared.errorBox}>{save.error instanceof ApiError ? save.error.message : 'Could not save the target.'}</div>
          )}
          {!isEdit && (
            <label className={shared.field}>
              <span className={shared.fieldLabel}>KIND</span>
              <select className={shared.input} value={kind} onChange={(e) => setKind(e.target.value as BackupTargetKind)}>
                <option value="http">http</option>
                <option value="scp">scp</option>
                <option value="ftp">ftp</option>
              </select>
            </label>
          )}
          <label className={shared.field}>
            <span className={shared.fieldLabel}>LABEL</span>
            <input type="text" className={shared.input} value={label} onChange={(e) => setLabel(e.target.value)} required />
          </label>
          {fieldsFor(kind).map((f) => (
            <label className={shared.field} key={f.key}>
              <span className={shared.fieldLabel}>{f.label.toUpperCase()}</span>
              <input
                type={f.secret ? 'password' : 'text'}
                className={shared.input}
                value={config[f.key] ?? ''}
                onChange={(e) => setField(f.key, e.target.value)}
                placeholder={isEdit && f.secret ? 'leave blank to keep unchanged' : undefined}
              />
            </label>
          ))}
        </div>
        <div className={shared.actions} style={{ padding: '0 22px 20px' }}>
          <button type="submit" className={shared.btnPrimary} disabled={save.isPending || !label.trim()}>
            {save.isPending ? 'SAVING…' : 'SAVE TARGET'}
          </button>
          <button type="button" className={shared.btn} onClick={onClose}>
            CANCEL
          </button>
        </div>
      </form>
    </Modal>
  )
}

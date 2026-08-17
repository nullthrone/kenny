import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../../api/client'
import EmptyState from '../../../components/EmptyState/EmptyState'
import { Download, ICON_STROKE_WIDTH } from '../../../components/icons'
import type { BackupEntry, BackupsResponse, BackupTarget, BackupVerifyResult } from '../types'
import shared from '../shared.module.css'
import styles from './BackupSection.module.css'
import RestoreConfirmModal from './RestoreConfirmModal'
import BackupTargetModal from './BackupTargetModal'

function formatSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** Admin → Backup. Full action set: create, verify, delete, download, restore, and remote targets. */
export default function BackupSection() {
  const queryClient = useQueryClient()
  const [restoreTarget, setRestoreTarget] = useState<BackupEntry | null>(null)
  const [restoreError, setRestoreError] = useState<string | null>(null)
  const [verifyResults, setVerifyResults] = useState<Record<string, BackupVerifyResult>>({})
  const [targetModal, setTargetModal] = useState<{ open: boolean; target: BackupTarget | null }>({ open: false, target: null })

  const query = useQuery({ queryKey: ['admin', 'backups'], queryFn: () => api.get<BackupsResponse>('/api/backups') })

  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ['admin', 'backups'] })
  }

  const create = useMutation({
    mutationFn: () => api.post<{ ok: boolean }>('/api/backups'),
    onSuccess: invalidate,
  })

  const verify = useMutation({
    mutationFn: (name: string) => api.post<BackupVerifyResult>(`/api/backups/${name}/verify`, { source: 'local' }),
    onSuccess: (result, name) => setVerifyResults((r) => ({ ...r, [name]: result })),
  })

  const del = useMutation({
    mutationFn: (name: string) => api.delete<{ ok: boolean }>(`/api/backups/${name}`),
    onSuccess: invalidate,
  })

  const restore = useMutation({
    mutationFn: (name: string) => api.post<{ ok: boolean; restarting: boolean }>(`/api/backups/${name}/restore`, { source: 'local' }),
    onSuccess: () => {
      setRestoreTarget(null)
      setRestoreError(null)
    },
    onError: (err) => setRestoreError(err instanceof ApiError ? err.message : 'Could not start the restore. Try again.'),
  })

  const testTarget = useMutation({
    mutationFn: (id: string) => api.post<{ ok: boolean; error?: string }>(`/api/backup-targets/${id}/test`),
  })

  const deleteTarget = useMutation({
    mutationFn: (id: string) => api.delete<{ ok: boolean }>(`/api/backup-targets/${id}`),
    onSuccess: invalidate,
  })

  if (query.isLoading) return <div className={shared.loading}>Loading…</div>
  if (query.isError) return <EmptyState title="Could not load backups" message="Something went wrong. Reload to try again." />
  if (!query.data) return null

  const { backups, config, targets } = query.data
  const actionError = [create, verify, del, testTarget, deleteTarget]
    .map((m) => (m.isError ? (m.error instanceof ApiError ? m.error.message : 'Something went wrong. Try again.') : null))
    .find((m) => m)

  return (
    <div>
      <p className={styles.intro}>
        {config.retention != null ? `Keeps ${config.retention} newest` : 'Automatic'}
        {config.interval_secs ? ` · every ${Math.round(config.interval_secs / 3600)} h` : ''}
        {config.backup_dir ? ` · ${config.backup_dir}` : ''}
      </p>

      {actionError && <div className={shared.errorBox}>{actionError}</div>}
      <div className={shared.actions} style={{ marginTop: 0, marginBottom: 16 }}>
        <button type="button" className={shared.btnPrimary} onClick={() => create.mutate()} disabled={create.isPending}>
          {create.isPending ? 'CREATING…' : 'CREATE BACKUP NOW'}
        </button>
      </div>

      {backups.length === 0 ? (
        <EmptyState title="No backups yet" message="A backup runs automatically on the schedule above, or create one now." />
      ) : (
        <div className={shared.table}>
          {backups.map((b) => {
            const result = verifyResults[b.name]
            return (
              <div key={b.name} className={shared.tableRow}>
                <div className={shared.tableMeta}>
                  <div className={`${shared.tableLabel} ${shared.mono}`}>{b.name}</div>
                  <div className={shared.tableSub}>
                    {new Date(b.created_at).toLocaleString()} · <span className={styles.sizeHint}>{formatSize(b.size)}</span> ·{' '}
                    {b.trigger} · {b.targets.map((t) => t.target).join(', ') || 'local'}
                  </div>
                  {result && (
                    <div className={styles.resultInline} style={{ color: result.ok ? 'var(--ok)' : 'var(--danger)' }}>
                      {result.ok ? 'verified ok' : `verify failed${result.error ? `: ${result.error}` : ''}`}
                    </div>
                  )}
                </div>
                <a className={styles.download} href={`/api/backups/${b.name}/download?source=local`}>
                  <Download width={14} height={14} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                  &nbsp;DOWNLOAD
                </a>
                <button type="button" className={shared.btnSmall} onClick={() => verify.mutate(b.name)} disabled={verify.isPending}>
                  VERIFY
                </button>
                <button
                  type="button"
                  className={shared.btnDanger}
                  onClick={() => {
                    setRestoreError(null)
                    setRestoreTarget(b)
                  }}
                >
                  RESTORE
                </button>
                <button type="button" className={shared.btnSmall} onClick={() => del.mutate(b.name)} disabled={del.isPending}>
                  DELETE
                </button>
              </div>
            )
          })}
        </div>
      )}

      <div className={styles.sectionHeading}>REMOTE TARGETS</div>
      {targets.length === 0 ? (
        <p className={shared.help}>No remote push target configured — backups stay local only.</p>
      ) : (
        <div className={shared.table}>
          {targets.map((t) => (
            <div key={t.id} className={shared.tableRow}>
              <div className={shared.tableMeta}>
                <div className={shared.tableLabel}>{t.label}</div>
                <div className={shared.tableSub}>{t.kind}</div>
              </div>
              <button type="button" className={shared.btnSmall} onClick={() => testTarget.mutate(t.id)} disabled={testTarget.isPending}>
                TEST
              </button>
              <button type="button" className={shared.btnSmall} onClick={() => setTargetModal({ open: true, target: t })}>
                EDIT
              </button>
              <button type="button" className={shared.btnDanger} onClick={() => deleteTarget.mutate(t.id)} disabled={deleteTarget.isPending}>
                REMOVE
              </button>
              {testTarget.data && testTarget.variables === t.id && (
                <div className={styles.resultInline} style={{ color: testTarget.data.ok ? 'var(--ok)' : 'var(--danger)' }}>
                  {testTarget.data.ok ? 'connection ok' : `failed${testTarget.data.error ? `: ${testTarget.data.error}` : ''}`}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      <div className={shared.actions}>
        <button type="button" className={shared.btn} onClick={() => setTargetModal({ open: true, target: null })}>
          ADD TARGET
        </button>
      </div>

      <RestoreConfirmModal
        backup={restoreTarget}
        onClose={() => {
          setRestoreTarget(null)
          setRestoreError(null)
        }}
        onConfirm={() => restoreTarget && restore.mutate(restoreTarget.name)}
        busy={restore.isPending}
        error={restoreError}
      />
      <BackupTargetModal
        open={targetModal.open}
        target={targetModal.target}
        onClose={() => setTargetModal({ open: false, target: null })}
      />
    </div>
  )
}

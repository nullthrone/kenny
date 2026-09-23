import { useMutation } from '@tanstack/react-query'
import { api, ApiError } from '../../../api/client'
import type { AdminRow } from '../types'
import GenericSettingsSection from './GenericSettingsSection'
import shared from '../shared.module.css'

interface ChannelResult {
  channel: string
  ok: boolean
  error: string | null
}

export interface AlertsSectionProps {
  rows: AdminRow[]
}

/**
 * Admin → Alerts & notifications. The alert cadence, the weekly digest and the
 * push channels, plus the two ways to check them without waiting for a real
 * alert: a test message through every configured channel, and the digest as it
 * would go out now.
 */
export default function AlertsSection({ rows }: AlertsSectionProps) {
  const test = useMutation({
    mutationFn: () => api.post<{ results: ChannelResult[] }>('/api/notify/test'),
  })
  const preview = useMutation({
    mutationFn: () => api.get<{ title: string; body: string }>('/api/digest/preview'),
  })

  const error = [test, preview].map((m) => (m.error instanceof ApiError ? m.error.message : m.isError ? 'Something went wrong. Try again.' : null)).find((m) => m)

  return (
    <div>
      <GenericSettingsSection rows={rows} />

      <div className={shared.actions}>
        <button type="button" className={shared.btn} onClick={() => test.mutate()} disabled={test.isPending}>
          {test.isPending ? 'SENDING…' : 'SEND TEST NOTIFICATION'}
        </button>
        <button type="button" className={shared.btn} onClick={() => preview.mutate()} disabled={preview.isPending}>
          {preview.isPending ? 'RENDERING…' : 'PREVIEW DIGEST'}
        </button>
      </div>

      {error && <div className={shared.errorBox}>{error}</div>}

      {test.data &&
        (test.data.results.length === 0 ? (
          <div className={shared.warnBox}>No channel is configured — set an ntfy topic or a webhook above.</div>
        ) : (
          <div className={shared.table}>
            {test.data.results.map((r) => (
              <div key={r.channel} className={shared.tableRow}>
                <div className={shared.tableMeta}>
                  <div className={shared.tableLabel}>{r.channel}</div>
                  <div className={shared.tableSub}>{r.ok ? 'delivered' : `failed · ${r.error ?? 'unknown error'}`}</div>
                </div>
              </div>
            ))}
          </div>
        ))}

      {preview.data && (
        <div className={shared.card}>
          <div className={shared.cardTitle}>{preview.data.title}</div>
          <pre className={shared.mono} style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
            {preview.data.body}
          </pre>
        </div>
      )}
    </div>
  )
}

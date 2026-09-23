import { useMutation } from '@tanstack/react-query'
import { api, ApiError } from '../../../api/client'
import { useAiStatus } from '../../../api/aiStatus'
import type { AdminRow } from '../types'
import GenericSettingsSection from './GenericSettingsSection'
import shared from '../shared.module.css'

export interface AiSectionProps {
  rows: AdminRow[]
}

/**
 * Admin → AI (ADR-0066). The Anthropic API key, a check that it works, the
 * models, and one switch per AI feature. Every feature is off while no key is
 * set, whatever its switch says — the status line says which ones may run now.
 */
export default function AiSection({ rows }: AiSectionProps) {
  const status = useAiStatus()
  const test = useMutation({
    mutationFn: () => api.post<{ ok: boolean; error: string | null }>('/api/ai/test'),
  })

  const configured = status.data?.configured === true
  const source = status.data?.source
  const running = status.data
    ? Object.entries(status.data.features ?? {})
        .filter(([, on]) => on)
        .map(([name]) => name.replace('_', ' '))
    : []

  return (
    <div>
      <p className={shared.help} style={{ marginBottom: 16 }}>
        {configured
          ? `Key ${source === 'db' ? 'saved here' : 'from the server environment'} · running: ${running.join(', ') || 'nothing'}`
          : 'No API key is set — every AI feature is off until one is.'}{' '}
        A key saved here is not included in backups; set it again after a restore.
      </p>

      <div className={shared.actions} style={{ marginTop: 0, marginBottom: 16 }}>
        <button type="button" className={shared.btn} onClick={() => test.mutate()} disabled={test.isPending || !configured}>
          {test.isPending ? 'TESTING…' : 'TEST KEY'}
        </button>
      </div>
      {test.data &&
        (test.data.ok ? (
          <div className={shared.okBox}>The key works.</div>
        ) : (
          <div className={shared.errorBox}>The key did not work: {test.data.error}</div>
        ))}
      {test.isError && (
        <div className={shared.errorBox}>{test.error instanceof ApiError ? test.error.message : 'Could not test the key.'}</div>
      )}

      <GenericSettingsSection rows={rows} />
    </div>
  )
}

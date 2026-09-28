import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../../api/client'
import { AI_STATUS_KEY, useAiStatus } from '../../../api/aiStatus'
import type { AdminRow } from '../types'
import { CONFIG_SOURCE_COLOR, CONFIG_SOURCE_LABEL } from '../types'
import GenericSettingsSection from './GenericSettingsSection'
import shared from '../shared.module.css'
import styles from './AiSection.module.css'

/** The master switch's setting (`kenny_server.ai.MASTER_SETTING`). */
export const MASTER_KEY = 'KENNY_AI_ENABLED'

/**
 * One click switches every AI feature off or back on. Each feature's own switch
 * keeps its setting, so turning this back on restores exactly what was running.
 */
function MasterSwitch({ row }: { row: AdminRow }) {
  const queryClient = useQueryClient()
  const on = row.value === true
  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ['settings'] })
    queryClient.invalidateQueries({ queryKey: AI_STATUS_KEY })
  }
  const save = useMutation({
    mutationFn: (value: boolean) => api.put(`/api/settings/${MASTER_KEY}`, { value }),
    onSuccess: invalidate,
  })
  const reset = useMutation({
    mutationFn: () => api.delete(`/api/settings/${MASTER_KEY}`),
    onSuccess: invalidate,
  })
  const busy = save.isPending || reset.isPending
  const error = save.error ?? reset.error

  return (
    <>
      <div className={styles.master}>
        <div className={styles.masterText}>
          <div className={styles.masterLabel}>{row.label.toUpperCase()}</div>
          <p className={shared.help}>
            {on
              ? 'On — each feature below follows its own switch.'
              : 'Off — no AI feature runs or appears anywhere, whatever its own switch says.'}
          </p>
        </div>
        <div className={styles.masterControls}>
          <span className={styles.source} style={{ color: CONFIG_SOURCE_COLOR[row.source] }}>
            {CONFIG_SOURCE_LABEL[row.source]}
          </span>
          {row.source === 'db' && (
            <button type="button" className={shared.btnSmall} onClick={() => reset.mutate()} disabled={busy}>
              RESET
            </button>
          )}
          <button
            type="button"
            role="switch"
            aria-checked={on}
            aria-label="AI features"
            className={styles.switch}
            onClick={() => save.mutate(!on)}
            disabled={busy || !row.editable}
          >
            <span className={styles.knob} />
          </button>
        </div>
      </div>
      {error && (
        <div className={shared.errorBox}>{error instanceof ApiError ? error.message : 'Could not save. Try again.'}</div>
      )}
    </>
  )
}

export interface AiSectionProps {
  rows: AdminRow[]
}

/**
 * Admin → AI (ADR-0066, ADR-0068). A master switch, the Anthropic API key, an
 * optional AI gateway, a check that the connection works, the models, and one
 * switch per AI feature. A feature runs only with the master switch on, a key or
 * gateway set and its own switch on — the status line says which ones may run
 * now, and whether calls go through a gateway.
 */
export default function AiSection({ rows }: AiSectionProps) {
  const status = useAiStatus()
  const test = useMutation({
    mutationFn: () => api.post<{ ok: boolean; error: string | null }>('/api/ai/test'),
  })

  const master = rows.find((row) => row.key === MASTER_KEY)
  const rest = rows.filter((row) => row.key !== MASTER_KEY)
  const aiOn = status.data?.enabled !== false
  const configured = status.data?.configured === true
  const source = status.data?.source
  const gateway = status.data?.gateway ?? null
  const credential =
    source === 'db'
      ? 'Key saved here'
      : source === 'env'
      ? 'Key from the server environment'
      : 'No key — the gateway authenticates'
  const route = gateway ? `via gateway ${gateway}` : 'direct to Anthropic'
  const running = status.data
    ? Object.entries(status.data.features ?? {})
        .filter(([, on]) => on)
        .map(([name]) => name.replace('_', ' '))
    : []

  return (
    <div>
      {master && <MasterSwitch row={master} />}

      <p className={shared.help} style={{ marginBottom: 16 }}>
        {!aiOn
          ? 'AI is switched off — no feature runs.'
          : configured
          ? `${credential} · ${route} · running: ${running.join(', ') || 'nothing'}`
          : 'No API key or gateway is set — every AI feature is off until one is.'}{' '}
        A key or gateway headers saved here are not included in backups; set them again after a restore.
      </p>

      <div className={shared.actions} style={{ marginTop: 0, marginBottom: 16 }}>
        <button type="button" className={shared.btn} onClick={() => test.mutate()} disabled={test.isPending || !configured}>
          {test.isPending ? 'TESTING…' : 'TEST CONNECTION'}
        </button>
      </div>
      {test.data &&
        (test.data.ok ? (
          <div className={shared.okBox}>The connection works.</div>
        ) : (
          <div className={shared.errorBox}>The connection failed: {test.data.error}</div>
        ))}
      {test.isError && (
        <div className={shared.errorBox}>{test.error instanceof ApiError ? test.error.message : 'Could not test the connection.'}</div>
      )}

      <GenericSettingsSection rows={rest} />
    </div>
  )
}

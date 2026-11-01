import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from '../../api/client'
import styles from './StewardCallback.module.css'

/** One row of `ticket_callbacks` (`stewards/callbacks.py::TicketCallback.to_public`). */
export interface TicketCallback {
  id: string
  ticket_id: string
  steward_id: string
  host_id: string
  effective_hash: string
  sections: string[]
  armed_by: string
  armed_at: string
  expires_at: string
  max_attempts: number
  attempts: number
  state: 'armed' | 'running' | 'verifying' | 'succeeded' | 'failed' | 'expired' | 'cancelled'
  last_run_id: string | null
  run_finished_at: string | null
  last_error: string
  ended_at: string | null
  ended_by: string | null
  live: boolean
}

/** A steward the callback could name, and whether it may be armed now. */
export interface CallbackSteward {
  steward_id: string
  title: string
  summary: string
  mode: string
  effective_hash: string
  allowed: boolean
  reason: string | null
  code: string | null
}

/** `GET /api/tickets/{id}/callback` (`TicketCallbacks.view`). */
export interface TicketCallbackView {
  ticket_id: string
  host_id: string | null
  sections: string[]
  callback: TicketCallback | null
  can_arm: boolean
  reason: string | null
  ttl_days: number
  max_attempts: number
  stewards: CallbackSteward[]
}

export const ticketCallbackKey = (id: string) => ['ticket', id, 'callback'] as const

const STATE_LABEL: Record<TicketCallback['state'], string> = {
  armed: 'waiting for the machine',
  running: 'running now',
  verifying: 'checking the result',
  succeeded: 'fixed',
  failed: 'did not fix it',
  expired: 'expired',
  cancelled: 'ended',
}

function formatTime(iso: string | null): string {
  if (!iso) return ''
  const ts = new Date(iso)
  return Number.isNaN(ts.getTime()) ? iso : ts.toLocaleString()
}

/**
 * Hand an alert ticket to a steward for the machine's next return (ADR-0075).
 *
 * The server decides everything shown here: which stewards may be named
 * (`stewards[].allowed`), whether this account may arm one (`can_arm`), and the
 * hash a person consents to (`effective_hash`, sent back as shown). Nothing is
 * inferred from the ticket. A ticket no server check can judge — anything a
 * person wrote — renders nothing.
 */
export default function StewardCallback({
  ticketId,
  onMutated,
}: {
  ticketId: string
  onMutated: () => void
}) {
  const queryClient = useQueryClient()
  const [chosen, setChosen] = useState<string>('')
  const [wanted, setWanted] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const view = useQuery({
    queryKey: ticketCallbackKey(ticketId),
    queryFn: () => api.get<TicketCallbackView>(`/api/tickets/${ticketId}/callback`),
    retry: false,
  })

  function settled() {
    void queryClient.invalidateQueries({ queryKey: ticketCallbackKey(ticketId) })
    onMutated()
  }

  function explain(err: unknown): string {
    if (err instanceof ApiError) {
      if (err.code === 'hash_stale') return 'The steward changed since this page loaded; review it again.'
      return err.message
    }
    return String(err)
  }

  const arm = useMutation({
    mutationFn: (steward: CallbackSteward) =>
      api.post<TicketCallback>(`/api/tickets/${ticketId}/callback`, {
        steward_id: steward.steward_id,
        effective_hash: steward.effective_hash,
      }),
    onSuccess: () => {
      setError(null)
      setWanted(false)
      settled()
    },
    onError: (err) => {
      setError(explain(err))
      void queryClient.invalidateQueries({ queryKey: ticketCallbackKey(ticketId) })
    },
  })

  const disarm = useMutation({
    mutationFn: () => api.delete<TicketCallback>(`/api/tickets/${ticketId}/callback`),
    onSuccess: () => {
      setError(null)
      settled()
    },
    onError: (err) => setError(explain(err)),
  })

  const data = view.data
  if (!data || !Array.isArray(data.sections) || !Array.isArray(data.stewards)) return null
  const current = data.callback
  if (data.sections.length === 0 && !current) return null

  const allowed = data.stewards.filter((s) => s.allowed)
  const selected = allowed.find((s) => s.steward_id === chosen) ?? allowed[0]
  const host = data.host_id ?? 'the machine'

  return (
    <section className={styles.root} data-shot="steward-callback">
      <h2 className={`kc-caps ${styles.heading}`}>Steward callback</h2>

      {current?.live && (
        <div className={styles.live}>
          <div>
            Handed to <strong>{current.steward_id}</strong> by {current.armed_by}: runs when {current.host_id}{' '}
            next sends telemetry — {STATE_LABEL[current.state]}. Attempt {current.attempts} of{' '}
            {current.max_attempts}, until {formatTime(current.expires_at)}.
          </div>
          {current.last_error && <div className={styles.muted}>Last: {current.last_error}</div>}
          <div className={styles.row}>
            <button
              type="button"
              className={`${styles.btn} kc-btn`}
              disabled={disarm.isPending}
              onClick={() => disarm.mutate()}
            >
              WITHDRAW
            </button>
          </div>
        </div>
      )}

      {current && !current.live && (
        <div className={styles.muted}>
          Last callback to {current.steward_id}: {STATE_LABEL[current.state]}
          {current.last_error ? ` — ${current.last_error}` : ''}.
        </div>
      )}

      {!current?.live && data.can_arm && data.stewards.length > 0 && (
        <div className={styles.form}>
          <label className={styles.check}>
            <input type="checkbox" checked={wanted} onChange={(e) => setWanted(e.target.checked)} />
            Let a steward fix {data.sections.join(', ')} when {host} is back
          </label>
          {wanted && (
            <>
              <select
                className={styles.select}
                aria-label="Steward"
                value={selected?.steward_id ?? ''}
                onChange={(e) => setChosen(e.target.value)}
              >
                {data.stewards.map((s) => (
                  <option key={s.steward_id} value={s.steward_id} disabled={!s.allowed}>
                    {s.title}
                    {s.allowed ? '' : ` — ${s.reason ?? 'not available'}`}
                  </option>
                ))}
              </select>
              <div className={styles.muted}>
                It runs at most {data.max_attempts} times within {data.ttl_days} days, as you, under the
                steward's own limits. It counts as fixed only when {data.sections.join(', ')} read healthy
                afterwards; if not, the alert channel is told.
              </div>
              <div className={styles.row}>
                <button
                  type="button"
                  className={`${styles.btn} ${styles.primary} kc-btn`}
                  disabled={!selected || arm.isPending}
                  onClick={() => selected && arm.mutate(selected)}
                >
                  HAND OVER
                </button>
              </div>
            </>
          )}
        </div>
      )}

      {!current?.live && !data.can_arm && data.reason && data.stewards.length > 0 && (
        <div className={styles.muted}>{data.reason}</div>
      )}

      {!current?.live && data.can_arm && data.stewards.length === 0 && (
        <div className={styles.muted}>
          No steward in act may work on {host} by command; none can be handed this ticket.
        </div>
      )}

      {error && <div className={styles.error}>{error}</div>}
    </section>
  )
}

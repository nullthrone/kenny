import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api/client'
import { consentErrorMessage } from '../../views/admin/sections/stewards/queries'
import { formatBytes } from '../../views/host/format'
import styles from './TicketDraftCard.module.css'
import own from './StewardRunProposalCard.module.css'
import { isFinished, summarizeRun, useStewardRunStatus } from './useStewardRunStatus'

/** `GET /api/stewards/{id}/command?host_id=` (`stewards/commands.py::readiness`). */
export interface CommandReadiness {
  steward_id: string
  host_id: string | null
  effective_hash: string | null
  mode: string
  preview: { allowed: boolean; reason: string | null }
  run: { allowed: boolean; reason: string | null; code: string | null }
  tools: CommandTool[]
}

/** A change-tier tool the run could use. */
export interface CommandTool {
  tool: string
  tier: string
  /** Per argument: the values it may take, or a sentence for values the server works out at run start. */
  values: Record<string, string[] | string>
  needs_authorization: boolean
  authorization: { id: string; remaining_today: number | null; expires_at: string | null } | null
}

export interface StewardRunProposalCardProps {
  itemId: string
  stewardId: string
  /** '' for a steward that is not bound to a PC. */
  hostId: string
  reason: string
  resolution: 'pending' | 'started' | 'dismissed'
  runId?: string
  /** Which button started the run. */
  startedMode?: 'preview' | 'act'
  /** The run's result has already been handed to the conversation. */
  resultReported?: boolean
  /** Starts the preview. Rejects with the server when it refuses (400/409/503). */
  onStart: (itemId: string, stewardId: string, hostId: string) => Promise<void>
  /** Starts a run that may act, naming the effective hash the card showed. Rejects with the server's refusal. */
  onRunNow: (itemId: string, stewardId: string, hostId: string, effectiveHash: string) => Promise<void>
  /** The run has reached a final status: tell the conversation (once per run). */
  onFinished: (itemId: string, runId: string) => void
  onDismiss: (itemId: string) => void
}

/**
 * What a refused start means to the person who pressed the button.
 *
 * A run that may act answers `403` for a caller who may not start one (the
 * consent helper's sentence), and `409 hash_stale` when the steward changed since
 * the card was loaded. Every other refusal (`host_not_idle`, `manual_run_running`,
 * a cap ...) carries its own reason, which is what is shown.
 */
export function runRefusalMessage(err: unknown, mode: 'preview' | 'act'): string {
  const e = err as { status?: number; code?: string; message?: string }
  if (mode === 'act') {
    if (e.status === 403) return consentErrorMessage(err, 'You may not start this run.')
    if (e.status === 409 && e.code === 'hash_stale') {
      return 'The steward changed since this card was loaded. What it may do is shown again below; review it, then press RUN NOW again.'
    }
  }
  return e.message || 'Could not start the run.'
}

function describeValues(values: CommandTool['values']): string {
  const parts = Object.entries(values).map(([arg, v]) =>
    typeof v === 'string' ? `${arg}: ${v}` : `${arg}: ${v.length ? v.join(', ') : 'nothing'}`,
  )
  return parts.join(' · ')
}

function describeAuthorization(tool: CommandTool): string {
  if (!tool.needs_authorization) return 'no authorization needed'
  const auth = tool.authorization
  if (!auth) return 'needs a standing authorization'
  const left = auth.remaining_today === null ? '' : `${auth.remaining_today} left today`
  const until = auth.expires_at ? `until ${auth.expires_at}` : 'no expiry'
  return ['authorized', left, until].filter(Boolean).join(' · ')
}

/**
 * The run kenny proposed (or the operator picked from a quick action), as a card
 * they accept or put away.
 *
 * Nothing has started when this renders. The card is the only thing that can
 * start anything, through `POST /api/stewards/{id}/runs` -- the route the
 * Stewards view uses -- so the run is started by the person who pressed the
 * button (ADR-0063: the copilot proposes, a person acts). PREVIEW is always a
 * shadow run and changes nothing on any PC. RUN NOW is a command run (ADR-0073)
 * in which the steward may act within its limits; it is offered only where the
 * server's readiness check (`GET /api/stewards/{id}/command`) says this caller
 * may, and it names the effective hash that check showed.
 *
 * Once started, the card follows the run until it finishes and then hands the
 * result to the conversation. It stays in the transcript once answered, the way
 * a draft ticket does.
 */
export default function StewardRunProposalCard({
  itemId,
  stewardId,
  hostId,
  reason,
  resolution,
  runId,
  startedMode,
  resultReported,
  onStart,
  onRunNow,
  onFinished,
  onDismiss,
}: StewardRunProposalCardProps) {
  const [title, setTitle] = useState(stewardId)
  const [busy, setBusy] = useState<'preview' | 'act' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [readiness, setReadiness] = useState<CommandReadiness | null>(null)
  const [readinessError, setReadinessError] = useState(false)

  // Fetched with the plain client, like the ticket draft's host list: the
  // drawer stays renderable without a query client. A failure only costs the
  // friendly name — the id is already on the card.
  useEffect(() => {
    let live = true
    api
      .get<{ stewards: { id: string; title: string }[] }>('/api/stewards')
      .then((res) => {
        const found = res.stewards.find((a) => a.id === stewardId)
        if (live && found?.title) setTitle(found.title)
      })
      .catch(() => undefined)
    return () => {
      live = false
    }
  }, [stewardId])

  const loadReadiness = useCallback(async () => {
    const query = hostId ? `?host_id=${encodeURIComponent(hostId)}` : ''
    try {
      const res = await api.get<CommandReadiness>(`/api/stewards/${encodeURIComponent(stewardId)}/command${query}`)
      if (!res || typeof res !== 'object' || !res.run || !res.preview) throw new Error('unreadable')
      setReadiness(res)
      setReadinessError(false)
    } catch {
      setReadiness(null)
      setReadinessError(true)
    }
  }, [stewardId, hostId])

  useEffect(() => {
    if (resolution !== 'pending') return undefined
    void loadReadiness()
    return undefined
  }, [resolution, loadReadiness])

  // Watching starts only for a run that has started; a pending or dismissed card reads nothing.
  const watch = useStewardRunStatus(resolution === 'started' && runId ? runId : null)
  const finished = watch.run !== null && isFinished(watch.run.status)
  const reportedRef = useRef(false)
  useEffect(() => {
    if (!finished || !runId || resultReported || reportedRef.current) return
    reportedRef.current = true
    onFinished(itemId, runId)
  }, [finished, runId, resultReported, itemId, onFinished])

  const stewardLink = `#/admin/stewards/${encodeURIComponent(stewardId)}`

  if (resolution === 'started') {
    const summary = watch.run ? summarizeRun(watch.run) : null
    return (
      <div className={styles.done}>
        {startedMode === 'act' ? 'Run started' : 'Preview started'} — run <code>{runId}</code> of{' '}
        <a className={styles.link} href={stewardLink}>
          {title}
        </a>
        {hostId ? ` on ${hostId}` : ''}
        <div className={own.status} role="status" aria-live="polite">
          {summary === null ? (
            watch.error ? (
              `Could not read the run yet: ${watch.error}`
            ) : (
              'Reading the run…'
            )
          ) : isFinished(summary.status) ? (
            <>
              {summary.status === 'completed' ? 'Finished' : summary.status === 'skipped' ? 'Skipped' : 'Failed'}
              {summary.verdict ? ` · verdict ${summary.verdict}` : ''}
              {` · ${summary.actions} ${summary.actions === 1 ? 'action' : 'actions'}`}
              {` · ${summary.recommendations} ${summary.recommendations === 1 ? 'recommendation' : 'recommendations'}`}
              {summary.bytesFreed !== null ? ` · ${formatBytes(summary.bytesFreed)} freed` : ''}
            </>
          ) : (
            <>
              Running…
              {` ${summary.actions} ${summary.actions === 1 ? 'action' : 'actions'} so far`}
              {summary.bytesFreed !== null ? ` · ${formatBytes(summary.bytesFreed)} freed` : ''}
              {watch.gaveUp ? ' · still going after 15 minutes; follow it in the Stewards view' : ''}
            </>
          )}
        </div>
      </div>
    )
  }

  if (resolution === 'dismissed') {
    return <div className={styles.done}>Run not started — {title}</div>
  }

  async function start(mode: 'preview' | 'act') {
    setBusy(mode)
    setError(null)
    try {
      if (mode === 'preview') {
        await onStart(itemId, stewardId, hostId)
      } else {
        await onRunNow(itemId, stewardId, hostId, readiness?.effective_hash ?? '')
      }
    } catch (e) {
      setError(runRefusalMessage(e, mode))
      // The steward changed: show what it may do now, with the hash a retry has to name.
      if (mode === 'act' && (e as { code?: string }).code === 'hash_stale') void loadReadiness()
    } finally {
      setBusy(null)
    }
  }

  const previewBlocked = readiness !== null && !readiness.preview.allowed
  const runAllowed = readiness !== null && readiness.run.allowed && !!readiness.effective_hash
  const runReason = readinessError
    ? 'Could not read whether this run may start.'
    : readiness === null
      ? 'Checking whether this run may start…'
      : readiness.run.allowed && !readiness.effective_hash
        ? 'The steward has no effective hash to run against.'
        : readiness.run.reason

  return (
    <div className={styles.card}>
      <p className={styles.heading}>PROPOSED RUN — nothing starts until you press a button</p>
      <dl className={own.facts}>
        <dt>STEWARD</dt>
        <dd>{title}</dd>
        <dt>PC</dt>
        <dd>{hostId || 'none — this steward is not bound to a PC'}</dd>
        {readiness && (
          <>
            <dt>MODE</dt>
            <dd>{readiness.mode}</dd>
          </>
        )}
        <dt>WHY</dt>
        <dd>{reason}</dd>
        {readiness && (
          <>
            <dt>MAY CHANGE</dt>
            <dd>
              {readiness.tools.length === 0 ? (
                'nothing — this steward has no tool that changes anything'
              ) : (
                <ul className={own.tools}>
                  {readiness.tools.map((t) => (
                    <li key={t.tool}>
                      <code>{t.tool}</code> ({t.tier})
                      {Object.keys(t.values).length > 0 && <> — {describeValues(t.values)}</>}
                      {' — '}
                      {describeAuthorization(t)}
                    </li>
                  ))}
                </ul>
              )}
            </dd>
          </>
        )}
      </dl>
      <p className={own.note}>
        PREVIEW runs the steward once in shadow: it reads, decides and reports, and every change it would make is only
        recorded as a recommendation. A preview never changes anything on any PC. RUN NOW lets it act, within the limits
        above. Either spends model tokens.
      </p>
      {previewBlocked && <p className={own.note}>Preview unavailable: {readiness?.preview.reason}</p>}
      {!runAllowed && runReason && <p className={own.note}>Run now unavailable: {runReason}</p>}
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
      <div className={`${styles.footer} kc-actions`}>
        <button
          type="button"
          className={`${styles.dismiss} kc-btn`}
          disabled={busy !== null}
          onClick={() => onDismiss(itemId)}
        >
          DISMISS
        </button>
        <button
          type="button"
          className={`${styles.dismiss} kc-btn`}
          disabled={busy !== null || previewBlocked}
          onClick={() => void start('preview')}
        >
          {busy === 'preview' ? 'STARTING…' : 'PREVIEW'}
        </button>
        <button
          type="button"
          className={`${styles.create} kc-btn`}
          disabled={busy !== null || !runAllowed}
          onClick={() => void start('act')}
        >
          {busy === 'act' ? 'STARTING…' : 'RUN NOW'}
        </button>
      </div>
    </div>
  )
}

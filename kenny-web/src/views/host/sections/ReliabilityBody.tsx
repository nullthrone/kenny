import { useState, type FormEvent } from 'react'
import type { ReliabilityEvent, ReliabilitySection } from '../types'
import { formatRelativeTime } from '../format'
import { useAddSuppression, useRemoveSuppression, useSuppressions } from '../api'
import styles from './ReliabilityBody.module.css'

export interface ReliabilityBodyProps {
  agentId: string
  reliability: ReliabilitySection
}

function levelColor(level: string): string {
  const l = level.toLowerCase()
  if (l === 'error' || l === 'critical' || l === 'crit') return 'var(--danger)'
  if (l === 'warn' || l === 'warning') return 'var(--warn)'
  return 'var(--text-muted)'
}

function EventCard({ agentId, event }: { agentId: string; event: ReliabilityEvent }) {
  const addSuppression = useAddSuppression()
  const removeSuppression = useRemoveSuppression()
  const color = levelColor(event.level)

  return (
    <div className={styles.eventCard}>
      <div className={styles.eventHead}>
        <span className={styles.source}>{event.source}</span>
        <span className={styles.levelChip} style={{ color }}>
          {event.level.toUpperCase()} · #{event.event_id}
        </span>
        <span className={styles.spacer} />
        <span className={styles.count}>{event.count}×</span>
      </div>
      {event.sample && <p className={styles.sample}>{event.sample}</p>}
      {event.suspected_cause && <p className={styles.cause}>{event.suspected_cause}</p>}
      <div className={styles.eventFoot}>
        <span className={styles.lastSeen}>last seen {formatRelativeTime(event.last_seen)}</span>
        <span className={styles.spacer} />
        {event.suppressed ? (
          <>
            <span className={styles.suppressedBadge}>
              SUPPRESSED{event.suppressed_by?.scope === 'fleet' ? ' · FLEET-WIDE' : ''}
            </span>
            {event.suppressed_by && (
              <button
                type="button"
                className={styles.suppressButton}
                disabled={removeSuppression.isPending}
                onClick={() => {
                  if (event.suppressed_by?.scope === 'fleet' && !window.confirm('Remove this fleet-wide suppression?')) return
                  removeSuppression.mutate(event.suppressed_by!.id)
                }}
              >
                UN-SUPPRESS
              </button>
            )}
          </>
        ) : (
          <button
            type="button"
            className={styles.suppressButton}
            disabled={addSuppression.isPending}
            onClick={() => addSuppression.mutate({ event_id: event.event_id, source: event.source, agent_id: agentId })}
          >
            SUPPRESS ON THIS HOST
          </button>
        )}
      </div>
      {(addSuppression.isError || removeSuppression.isError) && (
        <p className={styles.error}>Could not update the suppression rule.</p>
      )}
    </div>
  )
}

/** Full-edit reliability section modal body: the raw event breakdown from
 * `snapshot.reliability`, plus the suppression rules that mute a pattern
 * out of severity scoring (`GET/POST/DELETE /api/reliability/suppressions`).
 * `agent_id === ''` on a rule means fleet-wide. */
export default function ReliabilityBody({ agentId, reliability }: ReliabilityBodyProps) {
  const suppressions = useSuppressions()
  const addSuppression = useAddSuppression()
  const removeSuppression = useRemoveSuppression()

  const [source, setSource] = useState('')
  const [eventId, setEventId] = useState('')
  const [note, setNote] = useState('')
  const [scope, setScope] = useState<'host' | 'fleet'>('host')

  const relevantRules = (suppressions.data?.rules ?? []).filter((r) => r.agent_id === '' || r.agent_id === agentId)
  const events = reliability.events ?? []
  // A push whose probe failed carries no raw fields at all — only `status` and
  // `summary`. Distinguish that from a genuine reading of zero.
  const reading = reliability.recent_crashes !== undefined

  function onAddRule(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const eid = Number(eventId)
    if (!Number.isFinite(eid)) return
    addSuppression.mutate(
      { event_id: eid, source, agent_id: scope === 'host' ? agentId : '', note },
      { onSuccess: () => { setSource(''); setEventId(''); setNote('') } },
    )
  }

  return (
    <div>
      {reading ? (
        <p className={styles.summary}>
          Stability index {reliability.stability_index ?? '—'}/10 · {reliability.recent_crashes} error/critical
          events over {reliability.window_days} days.
        </p>
      ) : (
        <p className={styles.summary}>
          No reading: the collector could not read the event log on this push. This is not a
          clean bill of health — the last successful reading still stands.
        </p>
      )}

      <div className={styles.eyebrow}>EVENTS · {events.length}{reliability.truncated ? '+' : ''}</div>
      {events.length === 0 ? (
        <p className={styles.empty}>
          {reading ? 'No error/critical events in the window.' : 'No breakdown in this push.'}
        </p>
      ) : (
        <div className={styles.events}>
          {events.map((ev) => (
            <EventCard key={`${ev.source}-${ev.event_id}`} agentId={agentId} event={ev} />
          ))}
        </div>
      )}

      <div className={styles.eyebrow}>SUPPRESSION RULES</div>
      {relevantRules.length === 0 ? (
        <p className={styles.empty}>No suppression rules apply here yet.</p>
      ) : (
        <div className={styles.rules}>
          {relevantRules.map((r) => (
            <div key={r.id} className={styles.ruleRow}>
              <span className={styles.scopeChip}>{r.agent_id ? 'HOST' : 'FLEET'}</span>
              <span className={styles.ruleText}>
                {r.source || 'any source'} · #{r.event_id}
                {r.note && <span className={styles.ruleNote}> — {r.note}</span>}
              </span>
              <button
                type="button"
                className={styles.suppressButton}
                disabled={removeSuppression.isPending}
                onClick={() => {
                  if (!r.agent_id && !window.confirm('Remove this fleet-wide suppression?')) return
                  removeSuppression.mutate(r.id)
                }}
              >
                REMOVE
              </button>
            </div>
          ))}
        </div>
      )}

      <form className={styles.form} onSubmit={onAddRule}>
        <input
          className={styles.input}
          placeholder="source (blank = any)"
          value={source}
          onChange={(e) => setSource(e.target.value)}
        />
        <input
          className={styles.input}
          placeholder="event id"
          inputMode="numeric"
          value={eventId}
          onChange={(e) => setEventId(e.target.value)}
          style={{ width: 90 }}
        />
        <input className={styles.input} placeholder="note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
        <select className={styles.select} value={scope} onChange={(e) => setScope(e.target.value as 'host' | 'fleet')}>
          <option value="host">this host</option>
          <option value="fleet">fleet-wide</option>
        </select>
        <button type="submit" className={styles.addButton} disabled={addSuppression.isPending || !eventId.trim()}>
          ADD RULE
        </button>
      </form>
      {addSuppression.isError && <p className={styles.error}>Could not add that rule.</p>}
    </div>
  )
}

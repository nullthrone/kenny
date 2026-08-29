import { useMemo, useState, type FormEvent } from 'react'
import type { ReliabilityEvent, ReliabilitySection } from '../types'
import { formatRelativeTime } from '../format'
import { useAddSuppression, useRemoveSuppression, useSuppressions } from '../api'
import { buildHeatmap, groupByCategory, severityOf, shortDay, type EventSeverity } from './reliability'
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

/**
 * The categoriser's verdict, coloured by whether it needs you.
 *
 * `benign` is deliberately the quietest thing on the row: it is the whole point
 * of the annotation. A `critical`-level event judged benign should read as
 * settled, not as a second alarm next to the first.
 */
const SEVERITY_STYLE: Record<EventSeverity, { label: string; color: string }> = {
  serious: { label: 'SERIOUS', color: 'var(--danger)' },
  notable: { label: 'NOTABLE', color: 'var(--warn)' },
  benign: { label: 'BENIGN', color: 'var(--ok)' },
  unknown: { label: 'UNCLASSIFIED', color: 'var(--text-faint)' },
}

/**
 * Category x day grid over the collector's window.
 *
 * Shading is opacity against one colour, scaled to the busiest cell, and every
 * cell also carries its count as a `title` — density here is a shape to notice
 * (one bad day, or a steady drip), never a value to read off the colour.
 */
function Heatmap({ groups }: { groups: ReturnType<typeof groupByCategory> }) {
  const { days, rows, peak } = useMemo(() => buildHeatmap(groups), [groups])
  if (days.length === 0 || rows.length === 0) return null

  return (
    <div className={styles.heatmapWrap}>
      <table className={styles.heatmap}>
        <thead>
          <tr>
            <th scope="col" className={styles.heatmapCorner}>
              <span className={styles.srOnly}>Category</span>
            </th>
            {days.map((day) => (
              <th key={day} scope="col" className={styles.heatmapDay}>
                {shortDay(day)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.category}>
              <th scope="row" className={styles.heatmapRowLabel}>
                {row.category}
              </th>
              {row.counts.map((count, i) => (
                <td
                  key={days[i]}
                  className={styles.heatmapCell}
                  title={`${row.category} — ${days[i]}: ${count} event${count === 1 ? '' : 's'}`}
                  style={{ opacity: peak > 0 && count > 0 ? 0.2 + 0.8 * (count / peak) : undefined }}
                  data-empty={count === 0 ? 'true' : undefined}
                >
                  <span className={styles.srOnly}>{count}</span>
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function EventCard({ agentId, event }: { agentId: string; event: ReliabilityEvent }) {
  const addSuppression = useAddSuppression()
  const removeSuppression = useRemoveSuppression()
  const color = levelColor(event.level)
  const severity = SEVERITY_STYLE[severityOf(event)]

  return (
    <div className={styles.eventCard}>
      <div className={styles.eventHead}>
        <span className={styles.source}>{event.source}</span>
        <span className={styles.levelChip} style={{ color }}>
          {event.level.toUpperCase()} · #{event.event_id}
        </span>
        <span className={styles.severityChip} style={{ color: severity.color, borderColor: severity.color }}>
          {severity.label}
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

/**
 * Full-edit reliability section modal body: the raw event breakdown from
 * `snapshot.reliability`, plus the suppression rules that mute a pattern out of
 * severity scoring (`GET/POST/DELETE /api/reliability/suppressions`).
 * `agent_id === ''` on a rule means fleet-wide.
 *
 * The breakdown renders all three annotations the collector and categoriser send
 * (`docs/protocol.md`): `by_day` as the category x day heatmap, `category` as the
 * grouping, and `severity` as each row's badge. They are what turn a wall of
 * Windows event ids into a claim about the machine — a hundred `critical` rows
 * that are all one benign driver reads completely differently grouped and judged
 * than listed flat.
 */
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
  const groups = useMemo(() => groupByCategory(events), [events])
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
        <>
          {/* The grid first, the groups below it: which days were bad is the
              question you arrive with, and it answers before anything is expanded. */}
          <Heatmap groups={groups} />
          <div className={styles.groups}>
            {groups.map((group, i) => {
              const severity = SEVERITY_STYLE[group.worst]
              return (
                <details key={group.category} className={styles.group} open={i === 0}>
                  <summary className={styles.groupHead}>
                    <span className={styles.groupName}>{group.category}</span>
                    <span className={styles.severityChip} style={{ color: severity.color, borderColor: severity.color }}>
                      {severity.label}
                    </span>
                    <span className={styles.spacer} />
                    <span className={styles.count}>
                      {group.total}× · {group.events.length} pattern{group.events.length === 1 ? '' : 's'}
                    </span>
                  </summary>
                  <div className={styles.events}>
                    {group.events.map((ev) => (
                      <EventCard key={`${ev.source}-${ev.event_id}`} agentId={agentId} event={ev} />
                    ))}
                  </div>
                </details>
              )
            })}
          </div>
        </>
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

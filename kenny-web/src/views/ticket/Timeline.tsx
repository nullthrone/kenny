import type { DirectoryUser, TicketEvent } from './types'
import type { TriageFinding } from './eventFormat'
import { formatEvent, formatEventTime, triageFinding, verdictLabel, verdictTone } from './eventFormat'
import { useAddSuppression } from '../host/api'
import Markdown from '../../components/Markdown/Markdown'
import styles from './Timeline.module.css'

export interface TimelineProps {
  events: TicketEvent[]
  directory?: DirectoryUser[]
  /** The ticket's frozen host, for a suppression a verdict proposes. */
  agentId?: string | null
}

/**
 * A triage verdict, rendered as a finding rather than a line of history.
 *
 * The evidence sits next to the verdict on purpose: it is the reason to
 * believe it, and a verdict whose grounds are one click away is a verdict the
 * reader either takes on trust or re-derives themselves — both of which are
 * the work this feature exists to remove.
 *
 * `notResolvedBecause` is the other half of that. A verdict the server
 * declined to act on is not a failure to report; it is the most informative
 * row on the page while `KENNY_TRIAGE_RESOLVE` is still off, because it says
 * exactly what would have happened with it on.
 */
function Verdict({ finding, agentId }: { finding: TriageFinding; agentId?: string | null }) {
  const addSuppression = useAddSuppression()
  const tone = verdictTone(finding.verdict)
  const suggestion = finding.suggestion

  return (
    <div className={`${styles.verdict} ${styles[tone]}`} data-shot="triage-verdict">
      <div className={styles.verdictHead}>
        <span className={styles.verdictChip}>{verdictLabel(finding.verdict)}</span>
      </div>
      {finding.finding && <p className={styles.finding}>{finding.finding}</p>}
      {finding.evidence && <div className={styles.evidence}>checked: {finding.evidence}</div>}
      {finding.notResolvedBecause && (
        <div className={styles.withheld}>Not resolved: {finding.notResolvedBecause}</div>
      )}
      {suggestion && (
        <div className={styles.suggestion}>
          <span className={styles.suggestionText}>
            mute {suggestion.source} · #{suggestion.event_id}?
          </span>
          {addSuppression.isSuccess ? (
            <span className={styles.done}>MUTED</span>
          ) : (
            <button
              type="button"
              className={styles.suggestButton}
              disabled={addSuppression.isPending}
              onClick={() =>
                addSuppression.mutate({
                  event_id: suggestion.event_id,
                  source: suggestion.source,
                  // Host-scoped, not fleet-wide: the investigation looked at
                  // one machine and can only vouch for that one. A pattern
                  // that turns out to be harmless everywhere is widened from
                  // the Reliability panel, deliberately as a second decision.
                  agent_id: agentId ?? undefined,
                  note: `triage: ${finding.verdict}`,
                })
              }
            >
              MUTE ON THIS PC
            </button>
          )}
        </div>
      )}
      {addSuppression.isError && (
        <p className={styles.error}>Could not create the suppression rule.</p>
      )}
    </div>
  )
}

/**
 * The hairline timeline: a dot per event on a left rail, who/when, the
 * event's text, and — for tool/approval/error rows that carry one — a mono
 * block underneath. Tool args in a `mono` line are rendered exactly as
 * `formatEvent` produced them (verbatim `JSON.stringify`, same discipline
 * as a gate's frozen args): this is a historical trail entry, not an
 * editable value.
 *
 * How a row's text reads is `formatEvent`'s call, not this component's:
 * `f.body` says whether it is kenny's markdown, a person's verbatim message,
 * or a status line this UI composed.
 *
 * One row is not history: a triage verdict (see `Verdict`).
 */
export default function Timeline({ events, directory, agentId }: TimelineProps) {
  return (
    <div className={styles.rail} data-shot="ticket-timeline">
      {events.map((event) => {
        const f = formatEvent(event, directory)
        const finding = triageFinding(event)
        return (
          <div key={event.id} className={styles.entry}>
            <span className={styles.dot} style={{ background: f.dot }} />
            <div className={styles.headRow}>
              <span className={styles.who} style={{ color: f.whoColor }}>
                {f.who}
              </span>
              <span className={styles.time}>{formatEventTime(event.at)}</span>
            </div>
            {!finding &&
              (f.body === 'markdown' ? (
                <Markdown className={styles.text} text={f.text} />
              ) : (
                <div className={`${styles.text}${f.body === 'verbatim' ? ` ${styles.verbatim}` : ''}`}>
                  {f.text}
                </div>
              ))}
            {f.mono && <div className={styles.mono}>{f.mono}</div>}
            {finding && <Verdict finding={finding} agentId={agentId} />}
          </div>
        )
      })}
    </div>
  )
}

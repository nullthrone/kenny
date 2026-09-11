import type { DirectoryUser, TimelineEntry } from './types'
import type { TriageFinding } from './entryFormat'
import { actorColor, actorDot, actorLabel, formatEventTime } from './eventFormat'
import { findingOf, verdictLabel, verdictTone } from './entryFormat'
import { useAddSuppression } from '../host/api'
import Markdown from '../../components/Markdown/Markdown'
import styles from './Timeline.module.css'

export interface TimelineProps {
  entries: TimelineEntry[]
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
 * What happened on this ticket, read as a story: findings, what people said,
 * and what kenny changed — each already a sentence by the time it gets here.
 *
 * The reading is the server's (`kenny_server/ticket_timeline.py`), not this
 * component's: which trail rows condense into one line, which are dropped as
 * machine bookkeeping, and how each line is worded are one decision, made in
 * one place, so the ticket's other surfaces can reach the same words. This
 * renders `text` the way `body` says to, and nothing else.
 *
 * The raw rows are still there and still complete — `AuditTrail`, one tab
 * across. Nothing here is access control: both tabs answer to the same
 * ownership check.
 */
export default function Timeline({ entries, directory, agentId }: TimelineProps) {
  return (
    <div className={styles.rail} data-shot="ticket-timeline">
      {entries.map((entry) => {
        const finding = findingOf(entry)
        const muted = entry.kind === 'lifecycle'
        const key = entry.source_event_ids.join('-')
        return (
          <div key={key} className={styles.entry}>
            {!muted && (
              <span className={styles.dot} style={{ background: actorDot(entry.actor) }} />
            )}
            <div className={styles.headRow}>
              <span className={styles.who} style={{ color: actorColor(entry.actor) }}>
                {actorLabel(entry.actor, directory)}
              </span>
              <span className={styles.time}>{formatEventTime(entry.at)}</span>
            </div>
            {finding ? (
              <Verdict finding={finding} agentId={agentId} />
            ) : entry.body === 'markdown' ? (
              <Markdown className={styles.text} text={entry.text} />
            ) : (
              <div
                className={[
                  styles.text,
                  entry.body === 'verbatim' ? styles.verbatim : '',
                  muted ? styles.lifecycle : '',
                  entry.kind === 'problem' ? styles.problem : '',
                ]
                  .filter(Boolean)
                  .join(' ')}
              >
                {entry.text}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

import type { DirectoryUser, TicketEvent } from './types'
import { formatEvent, formatEventTime } from './eventFormat'
import styles from './Timeline.module.css'

export interface AuditTrailProps {
  events: TicketEvent[]
  directory?: DirectoryUser[]
}

/**
 * The ticket's trail exactly as the server stores it — every row, in order,
 * with the arguments a call ran with.
 *
 * This is the audit (ADR-0046), not the ticket's history: it answers "why did
 * this run?" and it is what `triage.may_resolve` reads to decide whether an
 * unprompted verdict may resolve a ticket (ADR-0056). `Timeline` is the same
 * rows read as a story; this is the same rows read as evidence, which is why
 * nothing here is condensed, collapsed or dropped.
 *
 * Nothing is parsed, either. An audit view shows what is stored: kenny's
 * markdown stays as the asterisks it is, and a tool name beginning with `-`
 * is not a bullet. The three surfaces that must render kenny's prose as
 * markdown are named in `src/test/markdownSamples.ts`, and this is not one of
 * them.
 *
 * Shares `Timeline.module.css`: it is literally the same rail, and two copies
 * of it would drift.
 */
export default function AuditTrail({ events, directory }: AuditTrailProps) {
  return (
    <div className={styles.rail} data-shot="ticket-audit">
      {events.map((event) => {
        const f = formatEvent(event, directory)
        return (
          <div key={event.id} className={styles.entry}>
            <span className={styles.dot} style={{ background: f.dot }} />
            <div className={styles.headRow}>
              <span className={styles.who} style={{ color: f.whoColor }}>
                {f.who}
              </span>
              <span className={styles.time}>{formatEventTime(event.at)}</span>
              <span className={styles.sourceId}>#{event.id}</span>
            </div>
            <div className={`${styles.text} ${styles.verbatim}`}>{f.text}</div>
            {f.mono && <div className={styles.mono}>{f.mono}</div>}
          </div>
        )
      })}
    </div>
  )
}

import type { DirectoryUser, TicketEvent } from './types'
import { formatEvent, formatEventTime } from './eventFormat'
import styles from './Timeline.module.css'

export interface TimelineProps {
  events: TicketEvent[]
  directory?: DirectoryUser[]
}

/**
 * The hairline timeline: a dot per event on a left rail, who/when, the
 * event's text, and — for tool/approval/error rows that carry one — a mono
 * block underneath. Tool args in a `mono` line are rendered exactly as
 * `formatEvent` produced them (verbatim `JSON.stringify`, same discipline
 * as a gate's frozen args): this is a historical trail entry, not an
 * editable value.
 */
export default function Timeline({ events, directory }: TimelineProps) {
  return (
    <div className={styles.rail}>
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
            </div>
            <div className={styles.text}>{f.text}</div>
            {f.mono && <div className={styles.mono}>{f.mono}</div>}
          </div>
        )
      })}
    </div>
  )
}

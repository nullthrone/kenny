import type { WebfilterEnforcement, WebfilterOversize, WebfilterScheduleState } from '../types'
import { formatClockTime, formatCountdown } from '../format'
import styles from './WebFilterStateBanner.module.css'

export interface OversizeCandidate {
  key: string
  label: string
  count: number
}

export interface WebFilterStateBannerProps {
  enforcement: WebfilterEnforcement
  schedule: WebfilterScheduleState
  oversize: WebfilterOversize | null
  oversizeCandidate: OversizeCandidate | null
}

/**
 * The one thing this modal has to make legible at a glance: is the stricter
 * list in force right now, and when does it revert — plus the over-cap
 * state, which is the other condition an operator must not have to infer
 * from a table. Both read directly off `schedule_state()`/`oversize`
 * (ADR-0055); nothing here re-derives a threshold.
 *
 * Precedence: an over-cap list is shown first — it means nothing is being
 * pushed at all, which matters more than which categories would apply if it
 * were. Otherwise: stricter-now, or the quiet base state.
 */
/** The clause appended to the banner for anything short of `protect`, so the
 * banner never implies a block that is not being pushed. */
function enforcementNote(enforcement: WebfilterEnforcement): string {
  if (enforcement === 'off') return ' Filtering is off for this host, so nothing is matched or enforced.'
  if (enforcement === 'log_only') return ' Log only: matches are recorded and alarmed, nothing is blocked.'
  return ''
}

export default function WebFilterStateBanner({
  enforcement,
  schedule,
  oversize,
  oversizeCandidate,
}: WebFilterStateBannerProps) {
  if (oversize) {
    return (
      <div className={`${styles.banner} ${styles.danger}`}>
        <div className={styles.label}>FILTER TOO LARGE TO ENFORCE</div>
        <p className={styles.text}>
          {oversize.count.toLocaleString()} domains — {oversize.over_by.toLocaleString()} over the{' '}
          {oversize.cap.toLocaleString()}-domain cap. Too large to push to this host. Monitoring continues: matching still
          flags every domain on the list, only the block itself did not go out.{' '}
          {oversizeCandidate
            ? `Turn off "${oversizeCandidate.label}" (${oversizeCandidate.count.toLocaleString()} domains, the largest enabled category) to bring it under the cap, or remove custom block entries.`
            : 'Turn off a category below to bring it under the cap, or remove custom block entries.'}
        </p>
      </div>
    )
  }

  if (schedule.stricter) {
    const clock = formatClockTime(schedule.next_change_local)
    const countdown = formatCountdown(schedule.reverts_at)
    return (
      <div className={`${styles.banner} ${styles.stricter}`}>
        <div className={styles.label}>STRICTER LIST IN FORCE</div>
        <p className={styles.text}>
          Reverts at {clock} ({schedule.timezone}) — {countdown ? `in ${countdown}` : 'shortly'}. Extra:{' '}
          {schedule.extra_categories.join(', ')}.
          {enforcementNote(enforcement)}
        </p>
      </div>
    )
  }

  const hasWindows = schedule.windows.length > 0
  const nextClock = formatClockTime(schedule.next_change_local)
  const nextCountdown = formatCountdown(schedule.next_change_at)

  return (
    <div className={`${styles.banner} ${styles.base}`}>
      <div className={styles.label}>BASE LIST IN FORCE</div>
      <p className={styles.text}>
        {hasWindows
          ? `No schedule window is open right now.${
              schedule.next_change_at ? ` Next window opens at ${nextClock} — in ${nextCountdown}.` : ''
            }`
          : 'No schedule configured for this host.'}
        {enforcementNote(enforcement)}
      </p>
    </div>
  )
}

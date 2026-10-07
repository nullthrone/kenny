import type { FanReading, FansSection } from '../types'
import Sparkline from '../../../components/Sparkline/Sparkline'
import { DASH, finite, formatCount, mean } from './hardwareFormat'
import { EmptyNote, Eyebrow, HwCard, HwCards, HwChip, Note, StatList, type StatItem } from './HardwareParts'
import styles from './FansBody.module.css'

export interface FansBodyProps {
  fans: FansSection
}

/** The burst's finite samples; a fan with none reads as "no measurement". */
function samplesOf(fan: FanReading): number[] {
  return (fan.rpm_samples ?? []).filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
}

export function formatRpm(value: number | null): string {
  return value === null ? DASH : `${formatCount(Math.round(value))} rpm`
}

function formatRange(samples: number[]): string {
  if (samples.length === 0) return DASH
  const lo = Math.min(...samples)
  const hi = Math.max(...samples)
  return lo === hi ? formatCount(lo) : `${formatCount(lo)}–${formatCount(hi)}`
}

function fanName(fan: FanReading): string {
  return fan.label || fan.key
}

function ActiveFan({ fan }: { fan: FanReading }) {
  const samples = samplesOf(fan)
  const duty = finite(fan.duty_percent)
  const items: StatItem[] = [
    { label: 'Mean speed', value: formatRpm(mean(samples)) },
    { label: 'Min–max', value: formatRange(samples) },
    { label: 'Duty', value: duty === null ? DASH : `${duty}%`, title: 'The commanded PWM duty, when the board exposes it.' },
    { label: 'Mode', value: fan.mode || DASH },
  ]
  return (
    <HwCard
      title={fanName(fan)}
      chips={
        <>
          {fan.source && <HwChip>{fan.source.toUpperCase()}</HwChip>}
          {fan.label && <span className={styles.key}>{fan.key}</span>}
        </>
      }
    >
      <div className={styles.body}>
        <StatList items={items} />
        {samples.length >= 2 && (
          <div className={styles.spark} title={`Burst of ${samples.length}: ${samples.join(', ')} rpm`}>
            <Sparkline values={samples} height={28} viewBoxWidth={100} />
          </div>
        )}
      </div>
    </HwCard>
  )
}

/**
 * Measured fan speeds (`snapshot.fans`, docs/protocol.md): a burst of RPM
 * samples per fan with the commanded duty where readable. The agent does not
 * grade this section; stall, jitter and drift judgements are the server's.
 */
export default function FansBody({ fans }: FansBodyProps) {
  const all = fans.fans ?? []
  const active = all.filter((f) => !f.idle_or_absent)
  const unused = all.filter((f) => f.idle_or_absent)
  const tried = fans.sources_tried ?? []
  const errors = fans.errors ?? []

  if (all.length === 0) {
    return (
      <div>
        <EmptyNote>
          No fan speed was read. Fan speeds are read on Windows only while LibreHardwareMonitor runs; on Linux from
          hwmon.
        </EmptyNote>
        {tried.length > 0 && <Note>Sources tried: {tried.join(', ')}.</Note>}
        {errors.length > 0 && (
          <ul className={styles.errors}>
            {errors.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        )}
      </div>
    )
  }

  return (
    <div>
      <Eyebrow>FANS · {active.length}{fans.truncated ? '+' : ''}</Eyebrow>
      {active.length === 0 ? (
        <EmptyNote>No fan is spinning on the channels this host reports.</EmptyNote>
      ) : (
        <HwCards>
          {active.map((fan) => (
            <ActiveFan key={fan.key} fan={fan} />
          ))}
        </HwCards>
      )}
      {fans.truncated && <Note>Only the first fans are listed.</Note>}

      {unused.length > 0 && (
        <details className={styles.unused}>
          <summary className={styles.unusedHead}>UNUSED CHANNELS · {unused.length}</summary>
          <p className={styles.unusedNote}>Every sample read 0 rpm and no duty is readable: nothing is attached, which is not a stall.</p>
          <ul className={styles.unusedList}>
            {unused.map((fan) => (
              <li key={fan.key}>
                {fanName(fan)}
                {fan.label && <span className={styles.key}> {fan.key}</span>}
                {fan.source && <span className={styles.key}> · {fan.source}</span>}
              </li>
            ))}
          </ul>
        </details>
      )}

      {errors.length > 0 && (
        <>
          <Eyebrow>PROBE ERRORS</Eyebrow>
          <ul className={styles.errors}>
            {errors.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}

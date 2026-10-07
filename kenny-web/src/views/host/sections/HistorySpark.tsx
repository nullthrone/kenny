import type { ReactNode } from 'react'
import type { HardwareSeriesPoint } from '../types'
import Sparkline from '../../../components/Sparkline/Sparkline'
import { formatCount } from './hardwareFormat'
import { lastValue, spanDays } from './hardwareHistory'
import styles from './HistorySpark.module.css'

export interface HistorySparkProps {
  /** What the series measures, e.g. "RPM at 50–70 % duty". The span is appended. */
  label: string
  points: HardwareSeriesPoint[]
  /** A fixed value to draw against the line, e.g. a spare-capacity threshold. */
  reference?: number
  /** `alert` colours a counter that has risen above zero. */
  tone?: 'alert'
  /** The latest value as text; defaults to a plain count. */
  format?: (value: number) => string
}

/** One long-lived series: a caption, the latest value and a sparkline. */
export function HistorySpark({ label, points, reference, tone, format = formatCount }: HistorySparkProps) {
  const caption = `${label} over ${spanDays(points)} days`
  return (
    <div className={styles.spark}>
      <div className={styles.head}>
        <span className={styles.label}>{caption}</span>
        <span className={styles.latest} data-tone={tone}>{format(lastValue(points))}</span>
      </div>
      <Sparkline
        values={points.map((p) => p.value)}
        height={32}
        viewBoxWidth={200}
        color={tone === 'alert' ? 'var(--danger)' : undefined}
        reference={reference}
        label={caption}
      />
    </div>
  )
}

/** The stack the sparklines of one device sit in. */
export function HistoryList({ children }: { children: ReactNode }) {
  return <div className={styles.list}>{children}</div>
}

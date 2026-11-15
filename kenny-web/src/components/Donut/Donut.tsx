import { useId } from 'react'
import type { DonutSegment } from '../../api/types'
import { severityColor } from '../tone'
import styles from './Donut.module.css'

export interface DonutProps {
  segments: DonutSegment[]
  size?: number
  className?: string
}

const R = 15.9 // prototype's radius trick: 2π×15.9 ≈ 99.9, so percentages ≈ dasharray units directly.
const CIRCUMFERENCE = 2 * Math.PI * R

/**
 * Hand-written inline SVG donut — no charting library, matching the
 * prototype's stacked-circle approach exactly (Kenny Console.dc.html line
 * 144): a background ring, then one `<circle>` per segment with
 * `stroke-dasharray`/`stroke-dashoffset` computed from each segment's
 * share of the total, all rotated -90° so the stack starts at 12 o'clock.
 */
export default function Donut({ segments, size = 76, className }: DonutProps) {
  const maskId = `donut-sweep-${useId().replace(/:/g, '')}`
  const total = segments.reduce((sum, s) => sum + s.value, 0)

  let cumulativePct = 0
  const arcs = total > 0
    ? segments
        .filter((s) => s.value > 0)
        .map((s) => {
          const pct = (s.value / total) * 100
          const dashoffset = cumulativePct === 0 ? undefined : -((cumulativePct / 100) * CIRCUMFERENCE)
          cumulativePct += pct
          return {
            key: s.key,
            color: severityColor(s.key),
            dasharray: `${(pct / 100) * CIRCUMFERENCE} ${CIRCUMFERENCE}`,
            dashoffset,
          }
        })
    : []

  return (
    <svg width={size} height={size} viewBox="0 0 42 42" className={className} role="img" aria-label="Fleet health breakdown">
      {/* The segments are revealed by one clockwise sweep from 12 o'clock: a
          mask ring whose stroke draws in once on mount. */}
      <mask id={maskId}>
        <circle
          cx="21"
          cy="21"
          r={R}
          fill="none"
          stroke="#fff"
          strokeWidth="7"
          pathLength={1}
          transform="rotate(-90 21 21)"
          className={styles.sweep}
        />
      </mask>
      <circle cx="21" cy="21" r={R} fill="none" stroke="var(--ink-100)" strokeWidth="6" />
      <g mask={`url(#${maskId})`}>
      {arcs.map((arc) => (
        <circle
          key={arc.key}
          cx="21"
          cy="21"
          r={R}
          fill="none"
          stroke={arc.color}
          strokeWidth="6"
          strokeDasharray={arc.dasharray}
          strokeDashoffset={arc.dashoffset}
          transform="rotate(-90 21 21)"
        />
      ))}
      </g>
    </svg>
  )
}

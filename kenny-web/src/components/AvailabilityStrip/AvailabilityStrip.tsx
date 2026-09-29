import type { FleetAvailability7d } from '../../api/types'
import { availabilityCellColor, formatAvailabilityPct } from '../availability'
import styles from './AvailabilityStrip.module.css'

export interface AvailabilityStripProps {
  /** `FleetAgent.availability_7d`; an older server omits it and nothing renders. */
  availability?: FleetAvailability7d
  className?: string
}

/**
 * A fleet card's last 7 days at a glance: one cell per hour, oldest on the
 * left, coloured on the same scale as the host page's band
 * (`availabilityCellColor`). Inline SVG with `%` geometry so it fills the
 * card at any width. Cells are ~1.6px wide on a card, so they are drawn with
 * crisp edges and overlap the next by a hair — antialiased, every boundary
 * showed as a faint seam.
 */
export default function AvailabilityStrip({ availability, className }: AvailabilityStripProps) {
  if (!availability || !Array.isArray(availability.cells) || availability.cells.length === 0) return null
  const { cells } = availability
  const step = 100 / cells.length
  const label = `7-day availability ${formatAvailabilityPct(availability.online_pct)}`

  return (
    <div className={`${styles.strip}${className ? ` ${className}` : ''}`} role="img" aria-label={label} title={label}>
      <svg width="100%" height="100%" aria-hidden="true" className={styles.svg} shapeRendering="crispEdges">
        {cells.map((value, i) => (
          <rect
            key={i}
            x={`${i * step}%`}
            y="0"
            width={`${step + 0.05}%`}
            height="100%"
            fill={availabilityCellColor(value)}
            data-cell=""
          />
        ))}
      </svg>
    </div>
  )
}

export interface SparklineProps {
  /**
   * Any numeric scale you like — normalized internally so the largest
   * value sits highest on the chart (standard sparkline convention: bigger
   * = higher, not "bigger = worse"). If a view wants a "badness" chart
   * where more attention-needed reads as a downward line, invert the
   * values before passing them in; this primitive has no health-specific
   * opinion baked in.
   */
  values: number[]
  height?: number
  /** Fixed viewBox width; the element itself is always `width="100%"` (prototype convention — scales to its container, non-uniformly if needed via `preserveAspectRatio="none"`). */
  viewBoxWidth?: number
  color?: string
  /** Area fill under the line, or omit for a stroke-only line (the Fleet host detail variant). */
  fill?: string
  /** Baseline rule at the bottom, matching the prototype's `<line>`. Default `var(--ink-200)`. */
  baselineColor?: string
  className?: string
}

/**
 * Hand-written inline SVG line/area chart — no charting library, matching
 * the prototype's approach (a `<path>` built from straight segments
 * between points, an optional fill under it, one baseline `<line>`).
 */
export default function Sparkline({
  values,
  height = 56,
  viewBoxWidth = 400,
  color = 'var(--green-600)',
  fill,
  baselineColor = 'var(--ink-200)',
  className,
}: SparklineProps) {
  const padding = 4
  const min = Math.min(...values)
  const max = Math.max(...values)
  const range = max - min

  const points = values.map((v, i) => {
    const x = values.length > 1 ? (i / (values.length - 1)) * viewBoxWidth : 0
    const y = range === 0 ? height / 2 : padding + (1 - (v - min) / range) * (height - padding * 2)
    return [x, y] as const
  })

  const linePath = points.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x},${y}`).join(' ')
  const areaPath = fill && points.length > 0
    ? `${linePath} L${viewBoxWidth},${height} L0,${height} Z`
    : null

  return (
    <svg
      width="100%"
      height={height}
      viewBox={`0 0 ${viewBoxWidth} ${height}`}
      preserveAspectRatio="none"
      className={className}
      role="img"
      aria-label="Trend"
    >
      {areaPath && <path d={areaPath} fill={fill} />}
      <path d={linePath} fill="none" stroke={color} strokeWidth="1.5" />
      <line x1="0" y1={height - 0.5} x2={viewBoxWidth} y2={height - 0.5} stroke={baselineColor} />
    </svg>
  )
}

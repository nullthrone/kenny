import styles from './Monogram.module.css'

/**
 * The Kenny "throne" monogram. Path data copied verbatim from the
 * prototype's inline `<svg viewBox="0 0 274 266">` (Kenny Console.dc.html,
 * sidebar at line 77 / logged-out screen at line 544 for the `full`
 * variant, header mobile-bar icon at line 112 for the `mark` variant).
 * Fills with `currentColor` — set `color` on a wrapper or pass `color`.
 */
export interface MonogramProps {
  /** `full` — the complete mark (sidebar, logged-out screen). `mark` — just the arch, used at 22×21 next to the mobile header title. */
  variant?: 'full' | 'mark'
  width?: number
  height?: number
  color?: string
  className?: string
  /**
   * `full` only: on mount the columns rise from their baseline, centre first
   * and then outward in pairs, like the StepRule. For the one logo a screen
   * opens with (the sidebar) — never for a mark that re-mounts per view.
   */
  animated?: boolean
}

/** Rise delay for a column `d` steps out from the centre. */
function riseDelay(d: number): { animationDelay: string } {
  return { animationDelay: `${d * 70}ms` }
}

export default function Monogram({
  variant = 'full',
  width = 30,
  height = 29,
  color,
  className,
  animated = false,
}: MonogramProps) {
  if (variant === 'mark') {
    return (
      <svg
        viewBox="0 0 274 266"
        width={width}
        height={height}
        style={color ? { color } : undefined}
        className={className}
        aria-hidden="true"
      >
        <g fill="currentColor">
          <path
            d="M61,21 H213 V266 H61 Z M92,97 A45,45 0 0 1 182,97 V199 A45,45 0 0 1 92,199 Z"
            fillRule="evenodd"
          />
        </g>
      </svg>
    )
  }
  const rise = animated ? styles.rise : undefined
  return (
    <svg
      viewBox="0 0 274 266"
      width={width}
      height={height}
      style={color ? { color } : undefined}
      className={className}
      aria-hidden="true"
    >
      <g fill="currentColor" fillRule="nonzero">
        {/* Each column sits in its own <g> so the rise animation's CSS
            transform never replaces a path's own mirroring transform. */}
        <g className={rise} style={animated ? riseDelay(0) : undefined}>
          <path d="M81,0 H193 V17 H81 Z" />
          <path
            fillRule="evenodd"
            d="M61,21 H213 V266 H61 Z M92,97 A45,45 0 0 1 182,97 V199 A45,45 0 0 1 92,199 Z"
          />
        </g>
        <g className={rise} style={animated ? riseDelay(1) : undefined}>
          <path d="M39,62 L56,50 V266 H39 Z" />
        </g>
        <g className={rise} style={animated ? riseDelay(1) : undefined}>
          <path transform="translate(274,0) scale(-1,1)" d="M39,62 L56,50 V266 H39 Z" />
        </g>
        <g className={rise} style={animated ? riseDelay(2) : undefined}>
          <path d="M17,106 L34,94 V266 H17 Z" />
        </g>
        <g className={rise} style={animated ? riseDelay(2) : undefined}>
          <path transform="translate(274,0) scale(-1,1)" d="M17,106 L34,94 V266 H17 Z" />
        </g>
        <path d="M0,257 H56 V266 H0 Z" />
        <path transform="translate(274,0) scale(-1,1)" d="M0,257 H56 V266 H0 Z" />
      </g>
    </svg>
  )
}

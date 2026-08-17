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
}

export default function Monogram({ variant = 'full', width = 30, height = 29, color, className }: MonogramProps) {
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
        <path d="M81,0 H193 V17 H81 Z" />
        <path
          fillRule="evenodd"
          d="M61,21 H213 V266 H61 Z M92,97 A45,45 0 0 1 182,97 V199 A45,45 0 0 1 92,199 Z"
        />
        <path d="M39,62 L56,50 V266 H39 Z" />
        <path transform="translate(274,0) scale(-1,1)" d="M39,62 L56,50 V266 H39 Z" />
        <path d="M17,106 L34,94 V266 H17 Z" />
        <path transform="translate(274,0) scale(-1,1)" d="M17,106 L34,94 V266 H17 Z" />
        <path d="M0,257 H56 V266 H0 Z" />
        <path transform="translate(274,0) scale(-1,1)" d="M0,257 H56 V266 H0 Z" />
      </g>
    </svg>
  )
}

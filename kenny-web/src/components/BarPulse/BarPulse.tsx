import styles from './BarPulse.module.css'

export interface BarPulseProps {
  /** 14 / 20 / 28px tall. */
  size?: 'sm' | 'md' | 'lg'
  /** `current` follows the surrounding text colour (ink panels, buttons). */
  tone?: 'ink' | 'brass' | 'current'
  /** Caps label beside the bars; also the accessible name. */
  label?: string
  /** Render the bars only, without a status role — when a visible label beside it already says what is happening. */
  decorative?: boolean
  className?: string
}

const HEIGHT = { sm: 14, md: 20, lg: 28 } as const

/**
 * Nullthrone's activity mark: three staggered bars echoing the monogram's
 * columns (`nt-bar-pulse`). Use it for work of unknown length that is
 * happening now — a running tool call, a view loading. A known fraction is a
 * progress bar instead.
 */
export default function BarPulse({ size = 'md', tone = 'ink', label, decorative = false, className }: BarPulseProps) {
  const h = HEIGHT[size]
  const w = Math.max(2, Math.round(h / 7))
  return (
    <span
      role={decorative ? undefined : 'status'}
      aria-label={decorative ? undefined : (label ?? 'Loading')}
      aria-hidden={decorative || undefined}
      className={`${styles.root} ${styles[tone]}${className ? ` ${className}` : ''}`}
    >
      <span className={styles.bars} style={{ height: h, gap: w }} aria-hidden="true">
        {[0, 1, 2].map((i) => (
          <span key={i} className={styles.bar} style={{ width: w, animationDelay: `${i * 150}ms` }} />
        ))}
      </span>
      {label && (
        <span className={`${styles.label} kc-caps`} aria-hidden="true">
          {label}
        </span>
      )}
    </span>
  )
}

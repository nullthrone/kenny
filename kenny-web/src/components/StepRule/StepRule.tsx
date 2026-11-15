import styles from './StepRule.module.css'

export interface StepRuleProps {
  /** `brass` is the one accent moment on a page — do not pair it with an accent button in the same band. */
  tone?: 'brass' | 'ink'
  size?: 'sm' | 'md'
  className?: string
}

/** Column heights, as fractions of the tallest — the monogram's stepped silhouette. */
const STEPS = [0.2, 0.4, 0.6, 1, 0.6, 0.4, 0.2]

/**
 * A hairline that rises in the middle into the stepped columns of the mark.
 * It divides major sections and closes a hero; use it at most once per view.
 * On mount the hairlines draw outward and the columns rise from the centre
 * out — the one ornamental entrance a view gets.
 */
export default function StepRule({ tone = 'brass', size = 'md', className }: StepRuleProps) {
  const col = size === 'sm' ? 6 : 8
  const hgt = size === 'sm' ? 18 : 28
  return (
    <div role="separator" className={`${styles.root} ${styles[tone]}${className ? ` ${className}` : ''}`}>
      <span className={`${styles.line} ${styles.lineLeft} kc-draw`} />
      <span className={styles.steps}>
        {STEPS.map((s, i) => (
          <span
            key={i}
            className={`${styles.step} kc-rise`}
            style={{
              width: col,
              height: Math.round(hgt * s),
              // Centre column first, then outward in pairs.
              animationDelay: `${120 + Math.abs(i - 3) * 60}ms`,
            }}
          />
        ))}
      </span>
      <span className={`${styles.line} ${styles.lineRight} kc-draw`} />
    </div>
  )
}

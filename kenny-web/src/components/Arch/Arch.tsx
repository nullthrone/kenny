import type { ReactNode } from 'react'
import styles from './Arch.module.css'

export interface ArchProps {
  width?: number | string
  /** Keep at least half the width, or the arch flattens. */
  height?: number | string
  /** `inset` (default), `card`, `brass` (highlighted content), `ink` (a monumental moment). */
  tone?: 'inset' | 'card' | 'brass' | 'ink'
  children?: ReactNode
  className?: string
}

/**
 * A container whose top is a semicircle, taken from the inner 0 of the mark.
 * For avatars, image masks and empty states — one or two per view.
 */
export default function Arch({ width = 160, height = 200, tone = 'inset', children, className }: ArchProps) {
  return (
    <div className={`${styles.root} ${styles[tone]}${className ? ` ${className}` : ''}`} style={{ width, height }}>
      {children}
    </div>
  )
}

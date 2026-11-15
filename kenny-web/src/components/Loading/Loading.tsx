import type { ReactNode } from 'react'
import BarPulse from '../BarPulse/BarPulse'
import styles from './Loading.module.css'

export interface LoadingProps {
  /** The visible sentence, e.g. "Loading the fleet…". */
  children?: ReactNode
  /** Spacing/type from the caller's own stylesheet; the bars and the gap come from here. */
  className?: string
}

/** A loading line: the BarPulse activity mark beside a plain sentence. */
export default function Loading({ children = 'Loading…', className }: LoadingProps) {
  return (
    <div role="status" className={`${styles.root}${className ? ` ${className}` : ''}`}>
      <BarPulse size="sm" tone="brass" decorative />
      <span>{children}</span>
    </div>
  )
}

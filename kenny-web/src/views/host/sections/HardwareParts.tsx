import type { ReactNode } from 'react'
import styles from './HardwareParts.module.css'

export type Tone = 'ok' | 'warn' | 'alert' | 'muted'

export function Eyebrow({ children }: { children: ReactNode }) {
  return <div className={styles.eyebrow}>{children}</div>
}

export function HwChip({ tone, children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={styles.chip} data-tone={tone} title={title}>
      {children}
    </span>
  )
}

export function Note({ tone, children }: { tone?: 'warn'; children: ReactNode }) {
  return (
    <p className={styles.note} data-tone={tone}>
      {children}
    </p>
  )
}

export function EmptyNote({ children }: { children: ReactNode }) {
  return <p className={styles.empty}>{children}</p>
}

export function Subhead({ children }: { children: ReactNode }) {
  return <div className={styles.subhead}>{children}</div>
}

export interface StatItem {
  label: string
  value: ReactNode
  /** `alert`/`warn` colour a counter that is above zero; `muted` is for noisy figures. */
  tone?: Tone
  title?: string
}

/** A definition list of label/value pairs. */
export function StatList({ items }: { items: StatItem[] }) {
  return (
    <dl className={styles.stats}>
      {items.map((item) => (
        <div key={item.label} className={styles.stat}>
          <dt className={styles.statLabel}>{item.label}</dt>
          <dd className={styles.statValue} data-tone={item.tone} title={item.title}>
            {item.value}
          </dd>
        </div>
      ))}
    </dl>
  )
}

export function HwCard({ title, chips, children }: { title: ReactNode; chips?: ReactNode; children: ReactNode }) {
  return (
    <div className={styles.card}>
      <div className={styles.head}>
        <span className={styles.title}>{title}</span>
        {chips}
      </div>
      {children}
    </div>
  )
}

export function HwCards({ children }: { children: ReactNode }) {
  return <div className={styles.cards}>{children}</div>
}

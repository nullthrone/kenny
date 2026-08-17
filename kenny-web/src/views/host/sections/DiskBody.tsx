import type { DiskSection, RawSection } from '../types'
import { severityColor } from '../../../components/tone'
import { formatBytes } from '../format'
import styles from './DiskBody.module.css'

export interface DiskBodyProps {
  disk: DiskSection
  diskSmart?: RawSection
}

/** Best-effort extraction of the mount `disk.reason` names ("C: 96% full
 * (>=95%)" → "C:"), so the one volume the server actually flagged can be
 * highlighted in the section's own colour. This reads the server's own
 * reason text; it never recomputes the 80/95% thresholds themselves — those
 * stay exclusively in `health_rules.py`. */
function worstMountPrefix(reason?: string): string | null {
  if (!reason) return null
  const m = reason.match(/^([A-Za-z]:|\/\S*)/)
  return m ? m[1] : null
}

export default function DiskBody({ disk, diskSmart }: DiskBodyProps) {
  const color = severityColor(disk.status)
  const worstMount = worstMountPrefix(disk.reason)

  return (
    <div>
      <div className={styles.volumes}>
        {disk.volumes.map((v) => {
          const highlighted = worstMount !== null && v.mount === worstMount
          const pct = Math.max(0, Math.min(100, v.percent_used))
          return (
            <div key={v.mount} className={styles.row}>
              <span className={styles.name}>{v.mount}</span>
              <span className={styles.bar}>
                <span
                  className={styles.fill}
                  style={{ width: `${pct}%`, background: highlighted ? color : 'var(--ink-200)' }}
                />
              </span>
              <span className={styles.used} style={{ color: highlighted ? color : 'var(--text-muted)' }}>
                {formatBytes(v.total_bytes - v.free_bytes)} / {formatBytes(v.total_bytes)} ({pct.toFixed(0)}%)
              </span>
            </div>
          )
        })}
      </div>

      {diskSmart?.summary && <p className={styles.smart}>SMART: {diskSmart.summary}</p>}

      {disk.top_dirs.length > 0 && (
        <>
          {/* The wire payload (docs/protocol.md "disk") carries current directory
              sizes, not a 30-day growth delta — unlike the prototype's demo
              flavour text ("+38 GB"), there's nothing to diff against here, so
              this reports what the contract actually has: current size. */}
          <div className={styles.eyebrow}>LARGEST DIRECTORIES</div>
          <div className={styles.dirs}>
            {disk.top_dirs.map((d) => (
              <div key={d.path}>
                {d.path} — {formatBytes(d.bytes)}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

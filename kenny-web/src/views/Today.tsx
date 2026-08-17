import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { api } from '../api/client'
import type { TodayItem, TodayResponse } from '../api/types'
import Donut from '../components/Donut/Donut'
import Sparkline from '../components/Sparkline/Sparkline'
import EmptyState from '../components/EmptyState/EmptyState'
import { severityColor } from '../components/tone'
import { splitVerdict, todayEyebrow, kpiColor, kpiValue } from './today/format'
import styles from './today/Today.module.css'

/** Today's row severity domain (`crit`/`warn`/`held`) isn't the shared `Severity`
 * union — `held` has no analogue there and needs the prototype's brass tone,
 * not warn's amber — so this stays a small local map rather than forcing the
 * value through `SeverityChip`. */
function rowSeverityColor(severity: TodayItem['severity']): string {
  if (severity === 'held') return 'var(--brass-600)'
  return severityColor(severity)
}

function rowSeverityLabel(severity: TodayItem['severity']): string {
  return severity.toUpperCase()
}

/** Strips the leading `#` the API's route strings carry (`#/fleet/oma-pc`) —
 * `HashRouter`'s own `<Link to>` values are plain paths, not hash literals. */
function toRoute(target: string): string {
  return target.startsWith('#') ? target.slice(1) : target
}

export default function Today() {
  const { data, isPending, isError, error } = useQuery({
    queryKey: ['today'],
    queryFn: () => api.get<TodayResponse>('/api/today'),
    // Mirrors the old Overview's 15s cache-staleness window (notes/view-endpoint-map.md,
    // "Today"): repaint instantly from cache on re-entry, only refetch past 15s.
    staleTime: 15_000,
  })

  if (isPending) {
    return (
      <div className={`${styles.root} kc-content kc-view`}>
        <h1 className="kc-h1">Today</h1>
      </div>
    )
  }

  if (isError && !data) {
    return (
      <div className={`${styles.root} kc-content kc-view`}>
        <EmptyState title="Could not load today" message={error instanceof Error ? error.message : 'Unknown error.'} />
      </div>
    )
  }
  if (!data) return null

  const [line1, line2] = splitVerdict(data.verdict_sentence)
  const allQuiet = data.items.length === 0

  return (
    <div className={`${styles.root} kc-content kc-view`}>
      <div className={styles.eyebrow}>{todayEyebrow(data.generated_at)}</div>
      <h1 className={`${styles.headline} kc-h1`}>
        {line1}
        {line2 && (
          <>
            <br />
            {line2}
          </>
        )}
      </h1>

      {allQuiet ? (
        <p className={styles.calm}>Nothing needs a decision from you right now.</p>
      ) : (
        <p className={styles.subtitle}>Ranked by consequence. Work top to bottom; the rest of the fleet can wait.</p>
      )}

      {!allQuiet && (
        <div className={styles.items}>
          {data.items.map((it, i) => (
            <Link
              key={`${it.target}-${i}`}
              to={toRoute(it.target)}
              className={`${styles.row} kc-todayrow kc-stagger-row`}
            >
              <span className={styles.sevBadge} style={{ color: rowSeverityColor(it.severity) }}>
                {rowSeverityLabel(it.severity)}
              </span>
              <span className="kc-cell">
                <span className={styles.rowTitle}>{it.host ? `${it.host} — ${it.title}` : it.title}</span>
                <span className={styles.rowDetail}>{it.detail}</span>
              </span>
              <span className={`${styles.rowAction} kc-rowaction`}>{it.action} →</span>
            </Link>
          ))}
        </div>
      )}

      <div className={`${styles.healthRow} kc-2col`}>
        <div className={styles.donutWrap}>
          <Donut segments={data.donut.segments} />
          <div className={styles.legend}>
            {data.donut.segments.map((seg) => (
              <div key={seg.key}>
                <span className={styles.legendDot} style={{ background: severityColor(seg.key) }} />
                {seg.value} {seg.label}
              </div>
            ))}
          </div>
        </div>
        <div>
          <div className={styles.trendHead}>
            <span className={styles.trendEyebrow}>FLEET HEALTH · 30 DAYS</span>
            <Link to="/fleet" className={styles.trendLink}>
              FULL FLEET →
            </Link>
          </div>
          <Sparkline
            values={data.trend_30d.days.map((d) => d.ok)}
            color="var(--green-600)"
            fill="var(--green-100)"
          />
        </div>
      </div>

      <div className={`${styles.kpis} kc-kpis`}>
        {data.kpis.map((kpi) => (
          <span key={kpi.key}>
            <span className={styles.kpiValue} style={{ color: kpiColor(kpi.severity) }}>
              {kpiValue(kpi)}
            </span>
            <span className={styles.kpiLabel}>{kpi.label}</span>
          </span>
        ))}
      </div>
    </div>
  )
}

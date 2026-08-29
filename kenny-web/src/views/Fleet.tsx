import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { api } from '../api/client'
import type { FleetResponse } from '../api/types'
import BinaryBanner from '../components/BinaryBanner/BinaryBanner'
import EmptyState from '../components/EmptyState/EmptyState'
import SeverityChip from '../components/SeverityChip/SeverityChip'
import { severityColor } from '../components/tone'
import { Monitor, ICON_STROKE_WIDTH } from '../components/icons'
import { osIcon, osLabel, relativePush } from './fleet/format'
import WizardMount from './fleet/WizardMount'
import styles from './fleet/Fleet.module.css'

export default function Fleet() {
  const { data, isPending, isError, error } = useQuery({
    queryKey: ['fleet'],
    queryFn: () => api.get<FleetResponse>('/api/fleet'),
  })

  return (
    <div className={`${styles.root} kc-content kc-view`}>
      <div className={styles.head}>
        <h1 className="kc-h1">The fleet</h1>
        <WizardMount />
      </div>

      {/* Above the grid and above Add a PC: an operator about to onboard a machine
          needs to know there is nothing to hand it before they start, not after
          the download 503s. */}
      <BinaryBanner />

      {isPending && <p style={{ color: 'var(--text-muted)', fontSize: 'var(--text-sm)' }}>Loading the fleet…</p>}

      {isError && !data && (
        <EmptyState
          icon={Monitor}
          title="Could not load the fleet"
          message={error instanceof Error ? error.message : 'Unknown error.'}
        />
      )}

      {data && data.agents.length === 0 && (
        <EmptyState icon={Monitor} title="No hosts yet" message="Add a PC to start monitoring it." />
      )}

      {data && data.agents.length > 0 && (
        <div className={styles.grid}>
          {data.agents.map((h) => {
            const OsIcon = osIcon(h.os)
            return (
              <Link key={h.agent_id} to={`/fleet/${h.agent_id}`} className={`${styles.card} kc-stagger-row`}>
                <div className={styles.cardTop}>
                  <span className={styles.dot} style={{ background: severityColor(h.overall) }} />
                  <span className={styles.hostname}>{h.agent_id}</span>
                  <OsIcon width={14} height={14} strokeWidth={ICON_STROKE_WIDTH} color="var(--text-faint)" aria-hidden="true" />
                </div>
                <SeverityChip severity={h.overall} label={h.severity_label} variant="text" className={styles.sevLabel} />
                <div className={styles.summary}>{h.summary}</div>
                <div className={styles.cardFoot}>
                  <span>{osLabel(h.os)}</span>
                  <span>push {relativePush(h.collected_at)}</span>
                </div>
              </Link>
            )
          })}
        </div>
      )}
    </div>
  )
}

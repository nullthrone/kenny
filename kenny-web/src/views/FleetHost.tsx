import { useState } from 'react'
import { Link, useParams } from 'react-router'
import type { HostSection } from '../api/types'
import EmptyState from '../components/EmptyState/EmptyState'
import Sparkline from '../components/Sparkline/Sparkline'
import { severityColor } from '../components/tone'
import { Monitor } from '../components/icons'
import { useAgentDetail } from './host/api'
import { normalizeSections } from './host/types'
import { osLabel, severityRank } from './host/format'
import ActionRow from './host/ActionRow'
import ForecastPanel from './host/ForecastPanel'
import SectionList from './host/SectionList'
import SectionModal from './host/SectionModal'
import ScreenshotCard from './host/ScreenshotCard'
import styles from './host/FleetHost.module.css'

export default function FleetHost() {
  const { host } = useParams<{ host: string }>()
  const agentId = host ?? ''
  const { data, isPending, isError, error } = useAgentDetail(agentId)
  const [openSection, setOpenSection] = useState<HostSection | null>(null)

  if (isPending) {
    return (
      <div className={`${styles.root} kc-content kc-view`}>
        <Link to="/fleet" className={styles.back}>
          ← FLEET
        </Link>
        <h1 className="kc-h1" style={{ marginTop: 14 }}>
          {agentId}
        </h1>
      </div>
    )
  }

  if (isError || !data) {
    return (
      <div className={`${styles.root} kc-content kc-view`}>
        <Link to="/fleet" className={styles.back}>
          ← FLEET
        </Link>
        <EmptyState
          icon={Monitor}
          title="Could not load this host"
          message={error instanceof Error ? error.message : 'Unknown error.'}
        />
      </div>
    )
  }

  const sections = normalizeSections(data.health.sections)
  const historyValues = data.history.map((h) => severityRank(h.overall))
  const trendDirection =
    historyValues.length >= 2
      ? historyValues[historyValues.length - 1] > historyValues[0]
        ? 'improving'
        : historyValues[historyValues.length - 1] < historyValues[0]
          ? 'degrading'
          : 'steady'
      : null

  return (
    <div className={`${styles.root} kc-content kc-view`}>
      <Link to="/fleet" className={styles.back}>
        ← FLEET
      </Link>

      <div className={styles.headRow}>
        <span className={styles.dot} style={{ background: severityColor(data.health.overall) }} />
        <h1 className="kc-h1">{data.agent_id}</h1>
        <span className={styles.meta}>
          {data.meta.version ? `v${data.meta.version}` : 'version unknown'} · {osLabel(data.os)} ·{' '}
          {data.online ? 'online' : 'offline'}
        </span>
      </div>

      <ActionRow agentId={data.agent_id} os={data.os} arch={data.meta.arch} channel={data.meta.channel} />

      <ForecastPanel agentId={data.agent_id} />

      <SectionList sections={sections} onOpenProblem={setOpenSection} />

      <div className={`${styles.bottomRow} kc-2col`}>
        <div>
          <div className={styles.trendEyebrow}>HEALTH · 30 DAYS</div>
          {historyValues.length > 0 ? (
            <>
              <Sparkline values={historyValues} color="var(--red-600)" />
              <p className={styles.trendCaption}>
                Worst-of health per snapshot{trendDirection ? ` — ${trendDirection} since the earliest reading shown` : ''}.
              </p>
            </>
          ) : (
            <p className={styles.trendEmpty}>Not enough history yet.</p>
          )}
        </div>
        <ScreenshotCard agentId={data.agent_id} />
      </div>

      <SectionModal
        agentId={data.agent_id}
        section={openSection}
        snapshot={data.snapshot}
        aiEnabled={data.ai_enabled}
        onClose={() => setOpenSection(null)}
      />
    </div>
  )
}

import type { ReactNode } from 'react'
import type { HostSection } from '../../api/types'
import Modal from '../../components/Modal/Modal'
import { X, ICON_STROKE_WIDTH } from '../../components/icons'
import { severityColor } from '../../components/tone'
import { sectionIcon, humanizeSectionName, isWebFilterSection, isAccountsSection, isReliabilitySection, isDiskSection } from './sections'
import { askKenny } from './askKenny'
import { useWebfilter } from './api'
import RecommendationBlock from './RecommendationBlock'
import WebFilterBody from './sections/WebFilterBody'
import AccountsBody from './sections/AccountsBody'
import ReliabilityBody from './sections/ReliabilityBody'
import DiskBody from './sections/DiskBody'
import GenericBody from './sections/GenericBody'
import type { DiskSection, LocalAccountsSection, RawSection, ReliabilitySection } from './types'
import styles from './SectionModal.module.css'

export interface SectionModalProps {
  agentId: string
  /** null closes the modal — problem cards are the only thing that opens one
   * (SectionList never wires a healthy cell to this). */
  section: HostSection | null
  snapshot: Record<string, RawSection> | null
  aiEnabled: boolean
  onClose: () => void
}

export default function SectionModal({ agentId, section, snapshot, aiEnabled, onClose }: SectionModalProps) {
  const isWebFilter = section !== null && isWebFilterSection(section.name)
  const webfilter = useWebfilter(agentId, isWebFilter)

  if (!section) return null

  const Icon = sectionIcon(section.name)
  const color = severityColor(section.status)
  const raw = snapshot?.[section.name]

  function remediate(prompt: string) {
    if (prompt) askKenny(prompt, agentId)
    onClose()
  }

  let body: ReactNode
  if (isWebFilterSection(section.name)) {
    if (webfilter.isPending) {
      body = <p className={styles.fallback}>Loading web filter configuration…</p>
    } else if (webfilter.isError || !webfilter.data) {
      body = (
        <p className={styles.fallback}>
          Could not load web filter configuration{webfilter.error instanceof Error ? `: ${webfilter.error.message}` : '.'}
        </p>
      )
    } else {
      body = <WebFilterBody agentId={agentId} overview={webfilter.data} />
    }
  } else if (isAccountsSection(section.name)) {
    const accounts = raw as LocalAccountsSection | undefined
    body = accounts?.accounts ? (
      <AccountsBody agentId={agentId} accounts={accounts} />
    ) : (
      <p className={styles.fallback}>No account inventory recorded for this host yet.</p>
    )
  } else if (isReliabilitySection(section.name)) {
    const reliability = raw as ReliabilitySection | undefined
    body = reliability?.events ? (
      <ReliabilityBody agentId={agentId} reliability={reliability} />
    ) : (
      <p className={styles.fallback}>No reliability data recorded for this host yet.</p>
    )
  } else if (isDiskSection(section.name)) {
    const disk = raw as DiskSection | undefined
    body = disk?.volumes ? (
      <DiskBody disk={disk} diskSmart={snapshot?.disk_smart} />
    ) : (
      <p className={styles.fallback}>No disk data recorded for this host yet.</p>
    )
  } else {
    body = raw ? <GenericBody data={raw} /> : <p className={styles.fallback}>No further telemetry recorded for this section yet.</p>
  }

  return (
    <Modal open onClose={onClose} labelledBy="section-modal-title" width={680}>
      <div className={styles.header}>
        <Icon width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} color={color} aria-hidden="true" />
        <span id="section-modal-title" className={styles.title}>
          {humanizeSectionName(section.name).toUpperCase()} · {agentId.toUpperCase()}
        </span>
        <span className={styles.rule} style={{ color }}>
          {section.reason ? `${section.reason} ⇒ ${section.status}` : section.status.toUpperCase()}
        </span>
        <button type="button" className={styles.close} onClick={onClose}>
          <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
        </button>
      </div>
      <div className={styles.body}>
        <RecommendationBlock agentId={agentId} sectionName={section.name} aiEnabled={aiEnabled} onRemediate={remediate} />
        {body}
      </div>
    </Modal>
  )
}

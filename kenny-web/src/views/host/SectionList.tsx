import type { HostSection } from '../../api/types'
import { Check, ICON_STROKE_WIDTH } from '../../components/icons'
import { severityColor } from '../../components/tone'
import { sectionIcon, humanizeSectionName } from './sections'
import styles from './SectionList.module.css'

export interface SectionListProps {
  sections: HostSection[]
  onOpenProblem: (section: HostSection) => void
}

/**
 * Splits problem cards from the healthy checklist purely on `section.attention`
 * — the server-computed split (`health_rules.py`), never re-derived from
 * `status` here. Only problem cards are clickable (the prototype's healthy
 * grid `<a>` rows carry no `onClick` at all, prototype lines 230-234).
 */
export default function SectionList({ sections, onOpenProblem }: SectionListProps) {
  const problems = sections.filter((s) => s.attention)
  const healthy = sections.filter((s) => !s.attention)

  return (
    <>
      {problems.length > 0 && (
        <>
          <div className={styles.eyebrow}>
            NEEDS ATTENTION · {problems.length} SECTION{problems.length === 1 ? '' : 'S'}
          </div>
          <div className={styles.problems}>
            {problems.map((s) => {
              const Icon = sectionIcon(s.name)
              const color = severityColor(s.status)
              return (
                <button
                  key={s.name}
                  type="button"
                  className={`${styles.problemCard} kc-stagger-row`}
                  onClick={() => onOpenProblem(s)}
                >
                  <div className={styles.problemHead}>
                    <Icon width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} color={color} aria-hidden="true" />
                    <span className={styles.problemName}>{humanizeSectionName(s.name)}</span>
                    <span className={styles.rule} style={{ color }}>
                      {s.reason ? `${s.reason} ⇒ ${s.status}` : s.status.toUpperCase()}
                    </span>
                  </div>
                  {s.summary && <div className={styles.problemSummary}>{s.summary}</div>}
                </button>
              )
            })}
          </div>
        </>
      )}

      <div className={styles.eyebrow}>
        HEALTHY · {healthy.length} SECTION{healthy.length === 1 ? '' : 'S'}
      </div>
      <div className={styles.healthyGrid}>
        {healthy.map((s) => (
          <div key={s.name} className={styles.healthyCell}>
            <Check width={13} height={13} strokeWidth={ICON_STROKE_WIDTH} color="var(--ok)" aria-hidden="true" />
            {humanizeSectionName(s.name)}
          </div>
        ))}
      </div>
    </>
  )
}

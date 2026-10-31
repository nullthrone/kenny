import { useState } from 'react'
import { Link } from 'react-router'
import type { StewardConstraint, Steward } from '../../types'
import AuthorizationsPanel from './AuthorizationsPanel'
import ModePanel from './ModePanel'
import ParamsPanel from './ParamsPanel'
import PreviewPanel from './PreviewPanel'
import RunsPanel from './RunsPanel'
import { ModeBadge, shortHash, TierBadge, triggerLabel } from './badges'
import shared from '../../shared.module.css'
import styles from './stewards.module.css'

export interface StewardDetailProps {
  steward: Steward
  canManage: boolean
}

function describeValues(values: unknown): string {
  if (Array.isArray(values)) return values.length === 0 ? 'none' : values.join(', ')
  return values === undefined || values === null ? 'none' : String(values)
}

/**
 * One constraint, with the kind it is: **literal** values the spec fixes, a **param** the
 * values of which an admin sets on this install, or **evidence** the server computes from
 * its own records at run start. In every kind an omitted or empty argument never matches.
 */
function ConstraintItem({ constraint, steward }: { constraint: StewardConstraint; steward: Steward }) {
  let kind: string
  let values: string
  if (constraint.param !== undefined) {
    kind = 'PARAM'
    values = `${constraint.param} — currently ${describeValues(steward.params[constraint.param])}`
  } else if (constraint.evidence !== undefined) {
    kind = 'EVIDENCE'
    values = `computed at run start from ${constraint.evidence}`
  } else {
    kind = 'LITERAL'
    values = describeValues(constraint.allowed)
  }
  return (
    <li>
      <span className={styles.kindTag}>{kind}</span>
      <span className={styles.toolName}>
        {constraint.tool}.{constraint.arg}
      </span>
      {' ∈ '}
      <span className="kc-evidence">{values}</span>
    </li>
  )
}

/** Admin → Stewards → one steward: what it is, then everything a person can do about it. */
export default function StewardDetail({ steward, canManage }: StewardDetailProps) {
  const [openRun, setOpenRun] = useState<string | null>(null)
  return (
    <div>
      <Link to="/admin/stewards" className={styles.back}>
        ← ALL STEWARDS
      </Link>
      <div className={styles.detailHead}>
        <h2 className={styles.detailTitle}>{steward.title}</h2>
        <ModeBadge steward={steward} />
      </div>
      <p className={shared.help} style={{ marginBottom: 20 }}>
        {steward.description}
      </p>

      <section className={shared.card} aria-labelledby={`spec-${steward.id}`}>
        <h3 className={styles.heading} id={`spec-${steward.id}`}>
          WHAT IT MAY DO
        </h3>
        <dl className={styles.dl}>
          <dt>TRIGGER</dt>
          <dd>{triggerLabel(steward)}</dd>
          <dt>EFFECTIVE HASH</dt>
          <dd>
            <span className={styles.hash} title={steward.effective_hash ?? undefined}>
              {shortHash(steward.effective_hash)}
            </span>{' '}
            <span className={shared.help} style={{ display: 'inline' }}>
              (the spec and its parameters together; act and every authorization are bound to it)
            </span>
          </dd>
          <dt>BUDGET</dt>
          <dd>{steward.budget.max_iterations} model round-trips per run</dd>
          <dt>TOOLS</dt>
          <dd>
            <ul className={styles.toolList} aria-label={`Tools of ${steward.title}`}>
              {steward.tools.map((tool) => (
                <li key={tool} className={styles.toolItem}>
                  <span className={styles.toolName}>{tool}</span>
                  <TierBadge tier={steward.tool_classes[tool]} />
                  {steward.verdict_tool === tool && <span className={shared.help}>reports the verdict</span>}
                </li>
              ))}
            </ul>
          </dd>
          <dt>CONSTRAINTS</dt>
          <dd>
            {steward.constraints.length === 0 ? (
              <span className={shared.help}>None: the steward has no change-tier tool to bind.</span>
            ) : (
              <ul className={styles.constraintList} aria-label="Argument constraints">
                {steward.constraints.map((c) => (
                  <ConstraintItem key={`${c.tool}.${c.arg}`} constraint={c} steward={steward} />
                ))}
              </ul>
            )}
          </dd>
          <dt>TIMEOUTS</dt>
          <dd>
            {steward.timeouts.length === 0 ? (
              <span className={shared.help}>Every call is bounded by the global ten minutes.</span>
            ) : (
              <ul className={styles.constraintList} aria-label="Per-tool timeouts">
                {steward.timeouts.map((t) => (
                  <li key={t.tool}>
                    <span className={styles.toolName}>{t.tool}</span> at most {t.max_s} s
                  </li>
                ))}
              </ul>
            )}
          </dd>
        </dl>
      </section>

      <ModePanel steward={steward} canManage={canManage} />
      <ParamsPanel steward={steward} canManage={canManage} />
      <AuthorizationsPanel steward={steward} canManage={canManage} />
      <PreviewPanel steward={steward} onStarted={setOpenRun} />
      <RunsPanel steward={steward} selected={openRun} onSelect={setOpenRun} />
    </div>
  )
}

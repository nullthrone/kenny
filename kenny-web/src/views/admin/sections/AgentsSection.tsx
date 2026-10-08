import { Link, useParams } from 'react-router'
import { ApiError } from '../../../api/client'
import EmptyState from '../../../components/EmptyState/EmptyState'
import { formatRelativeTime } from '../../host/format'
import type { SpecializedAgent } from '../types'
import AgentDetail from './agents/AgentDetail'
import { ModeBadge, RunStatusTag, shortHash, triggerLabel } from './agents/badges'
import { useAgents } from './agents/queries'
import shared from '../shared.module.css'
import styles from './agents/agents.module.css'

export interface AgentsSectionProps {
  /**
   * Whether this account may change an agent: choose its mode, edit its parameters,
   * grant or revoke an authorization. Superuser only (ADR-0072 rule 2). The server
   * enforces it again, and additionally refuses anything but a person signed in at
   * a browser — this only decides which controls are drawn.
   */
  canManage: boolean
}

function AgentRow({ agent }: { agent: SpecializedAgent }) {
  const latest = agent.latest_run
  return (
    <li className={styles.agentRow}>
      <div>
        <Link to={`/admin/agents/${encodeURIComponent(agent.id)}`} className={styles.agentTitle}>
          {agent.title}
        </Link>
        <p className={styles.desc}>{agent.description}</p>
        <div className={styles.facts}>
          <span>trigger: {triggerLabel(agent)}</span>
          <span>
            hash <span className={styles.hash}>{shortHash(agent.effective_hash)}</span>
          </span>
          <span>
            {latest ? (
              <>
                last run {formatRelativeTime(latest.started_at)}
                {latest.verdict ? ` · ${latest.verdict}` : ''}
              </>
            ) : (
              'no runs yet'
            )}
          </span>
        </div>
      </div>
      <div className={styles.rowSide}>
        <ModeBadge agent={agent} />
        {latest && <RunStatusTag status={latest.status} />}
      </div>
    </li>
  )
}

/**
 * Admin → Specialized agents (ADR-0071, ADR-0072). Operators and up read the catalog,
 * every agent's mode, parameters, authorizations and runs, and may start a preview;
 * a superuser also changes them. `#/admin/agents` is the list, `#/admin/agents/{id}`
 * one agent.
 */
export default function AgentsSection({ canManage }: AgentsSectionProps) {
  const { detail } = useParams<{ detail?: string }>()
  const query = useAgents()

  if (query.isLoading) return <div className={shared.loading}>Loading…</div>
  if (query.isError || !query.data) {
    const unavailable = query.error instanceof ApiError && query.error.status === 503
    return (
      <EmptyState
        title={unavailable ? 'Specialized agents are not configured' : 'Could not load the agents'}
        message={unavailable ? 'This server was started without them.' : 'Something went wrong. Reload to try again.'}
      />
    )
  }

  const { enabled, agents } = query.data

  const banner = !enabled && (
    <div className={`${shared.warnBox} ${styles.banner}`} role="status">
      Specialized agents are switched off for the whole install, so none of them starts, whatever its mode says.
      A superuser turns them back on under Admin → AI.
    </div>
  )

  if (detail) {
    const agent = agents.find((a) => a.id === detail)
    return (
      <div>
        {banner}
        {agent ? (
          <AgentDetail agent={agent} canManage={canManage} />
        ) : (
          <>
            <Link to="/admin/agents" className={styles.back}>
              ← ALL AGENTS
            </Link>
            <EmptyState title="Unknown agent" message={`There is no agent called ${detail}.`} />
          </>
        )}
      </div>
    )
  }

  return (
    <div>
      {banner}
      <p className={shared.help} style={{ marginBottom: 12 }}>
        Agents are tasks kenny runs on its own, each with a closed set of tools. In <strong>shadow</strong> an
        agent does the whole job but every change is only recorded as a recommendation; in <strong>act</strong>{' '}
        it makes the changes its limits allow.
      </p>
      {agents.length === 0 ? (
        <EmptyState title="No agents" message="This server ships none." />
      ) : (
        <ul className={shared.table} style={{ listStyle: 'none', margin: 0, padding: 0 }} aria-label="Specialized agents">
          {agents.map((a) => (
            <AgentRow key={a.id} agent={a} />
          ))}
        </ul>
      )}
    </div>
  )
}

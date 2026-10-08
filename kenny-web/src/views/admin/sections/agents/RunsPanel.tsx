import { useQueryClient } from '@tanstack/react-query'
import EmptyState from '../../../../components/EmptyState/EmptyState'
import { formatRelativeTime } from '../../../host/format'
import type { AgentCall, AgentRun, SpecializedAgent } from '../../types'
import { RunStatusTag, shortHash, TierBadge } from './badges'
import { AGENTS_KEY, useAgentRun, useAgentRuns } from './queries'
import shared from '../../shared.module.css'
import styles from './agents.module.css'

function totalTokens(run: AgentRun): number {
  return run.input_tokens + run.output_tokens + run.cache_read_tokens + run.cache_creation_tokens
}

function subjectOf(run: AgentRun): string {
  return run.host_id ?? run.ticket_id ?? run.subject ?? '—'
}

function CallsTable({ calls, label }: { calls: AgentCall[]; label: string }) {
  return (
    <div className={styles.scroller} role="region" aria-label={label} tabIndex={0}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">TOOL</th>
            <th scope="col">ARGUMENTS</th>
            <th scope="col">TIER</th>
            <th scope="col">RESULT</th>
            <th scope="col">AUTHORIZATION</th>
          </tr>
        </thead>
        <tbody>
          {calls.map((c, i) => (
            <tr key={`${c.tool}-${i}`}>
              <td className={styles.cellBreak}>
                <span className={styles.toolName}>{c.tool}</span>
                {c.agent_id && <div className={shared.tableSub}>on {c.agent_id}</div>}
              </td>
              <td className={styles.cellBreak}>
                <pre className={styles.pre}>{JSON.stringify(c.args ?? {})}</pre>
              </td>
              <td>
                <TierBadge tier={c.tool_class} />
              </td>
              <td className={styles.cellBreak}>{c.ok === undefined ? '—' : c.ok ? 'ok' : `failed${c.code ? ` (${c.code})` : ''}`}</td>
              <td className={styles.cellBreak}>
                {c.authorization_id ? <span className={styles.hash}>{c.authorization_id}</span> : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** One run, in full: what it did, what it only proposed, how it ended and what it cost. */
export function RunDetail({ runId }: { runId: string }) {
  const queryClient = useQueryClient()
  const query = useAgentRun(runId)

  if (query.isLoading) return <div className={shared.loading}>Loading…</div>
  if (query.isError || !query.data) {
    return (
      <EmptyState
        title="Could not load this run"
        message="It may not have been written yet, or it was pruned. Reload to try again."
        action={{ label: 'RELOAD', onClick: () => queryClient.invalidateQueries({ queryKey: AGENTS_KEY }) }}
      />
    )
  }
  const run = query.data
  return (
    <div className={styles.runDetail} role="region" aria-label={`Run ${run.id}`}>
      <dl className={styles.dl}>
        <dt>STATUS</dt>
        <dd>
          <RunStatusTag status={run.status} /> in {run.mode}
          {run.mode !== 'act' && ' (nothing was changed)'}
        </dd>
        <dt>STARTED</dt>
        <dd>
          {formatRelativeTime(run.started_at)} by {run.trigger}
          {run.finished_at ? `, finished ${formatRelativeTime(run.finished_at)}` : ''}
        </dd>
        <dt>SUBJECT</dt>
        <dd className="kc-evidence">{subjectOf(run)}</dd>
        <dt>VERSION</dt>
        <dd>
          effective hash <span className={styles.hash}>{shortHash(run.effective_hash)}</span>, spec{' '}
          <span className={styles.hash}>{shortHash(run.spec_hash)}</span>
        </dd>
        <dt>VERDICT</dt>
        <dd>{run.verdict ?? '—'}</dd>
        <dt>FINDING</dt>
        <dd>{run.summary ? <p className={styles.summary}>{run.summary}</p> : '—'}</dd>
        {run.error && (
          <>
            <dt>ERROR</dt>
            <dd className="kc-evidence">{run.error}</dd>
          </>
        )}
        <dt>TOKENS</dt>
        <dd>
          {totalTokens(run).toLocaleString()} in total — {run.input_tokens.toLocaleString()} in,{' '}
          {run.output_tokens.toLocaleString()} out, {run.cache_read_tokens.toLocaleString()} cache read,{' '}
          {run.cache_creation_tokens.toLocaleString()} cache write
        </dd>
      </dl>

      <h4 className={styles.sub}>ACTIONS ({run.actions.length})</h4>
      {run.actions.length === 0 ? (
        <p className={shared.help}>No tool call of this run.</p>
      ) : (
        <CallsTable calls={run.actions} label="Actions of this run" />
      )}

      <h4 className={styles.sub}>RECOMMENDATIONS ({run.recommendations.length})</h4>
      {run.recommendations.length === 0 ? (
        <p className={shared.help}>Nothing was proposed and refused.</p>
      ) : (
        <>
          <p className={shared.help} style={{ marginBottom: 8 }}>
            Changes the run wanted to make and was not allowed to. A person can do them through the ordinary confirm step, as themselves.
          </p>
          <CallsTable calls={run.recommendations} label="Recommendations of this run" />
        </>
      )}
    </div>
  )
}

export interface RunsPanelProps {
  agent: SpecializedAgent
  selected: string | null
  onSelect: (runId: string | null) => void
}

/** The agent's runs, newest first; a row opens its record below the list. */
export default function RunsPanel({ agent, selected, onSelect }: RunsPanelProps) {
  const query = useAgentRuns(agent.id)
  const headingId = `runs-${agent.id}`

  let body
  if (query.isLoading) body = <div className={shared.loading}>Loading…</div>
  else if (query.isError || !query.data) {
    body = <EmptyState title="Could not load the runs" message="Something went wrong. Reload to try again." />
  } else if (query.data.length === 0 && !selected) {
    body = <p className={shared.help}>This agent has not run yet.</p>
  } else {
    body = (
      <div className={styles.scroller} role="region" aria-label="Runs" tabIndex={0}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th scope="col">STARTED</th>
              <th scope="col">SUBJECT</th>
              <th scope="col">MODE</th>
              <th scope="col">STATUS</th>
              <th scope="col">VERDICT</th>
              <th scope="col">TOKENS</th>
              <th scope="col" aria-label="Open" />
            </tr>
          </thead>
          <tbody>
            {query.data.map((run) => (
              <tr key={run.id} className={selected === run.id ? styles.selectedRow : undefined}>
                <td>{formatRelativeTime(run.started_at)}</td>
                <td className={styles.cellBreak}>{subjectOf(run)}</td>
                <td>{run.mode}</td>
                <td>
                  <RunStatusTag status={run.status} />
                </td>
                <td className={styles.cellBreak}>{run.verdict ?? '—'}</td>
                <td>{totalTokens(run).toLocaleString()}</td>
                <td>
                  <button
                    type="button"
                    className={styles.linkButton}
                    aria-expanded={selected === run.id}
                    aria-label={`${selected === run.id ? 'Close' : 'Open'} the run from ${formatRelativeTime(run.started_at)} on ${subjectOf(run)}`}
                    onClick={() => onSelect(selected === run.id ? null : run.id)}
                  >
                    {selected === run.id ? 'CLOSE' : 'DETAILS'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  return (
    <section className={shared.card} aria-labelledby={headingId}>
      <h3 className={styles.heading} id={headingId}>
        RUNS
      </h3>
      {body}
      {selected && <RunDetail runId={selected} />}
    </section>
  )
}

import { useState, type FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../../../../api/client'
import EmptyState from '../../../../components/EmptyState/EmptyState'
import type { AgentAuthorization, SpecializedAgent } from '../../types'
import { AuthorizationStateTag, MAX_EXPIRY_DAYS, NEVER_AUTHORIZED, shortHash } from './badges'
import { AGENTS_KEY, consentErrorMessage, isHashChanged, useAgentAuthorizations, useFleetHosts } from './queries'
import shared from '../../shared.module.css'
import styles from './agents.module.css'

const DAY_MS = 24 * 60 * 60 * 1000
const SERVER_SCOPE = 'server'
const DEFAULT_EXPIRY_DAYS = 30
const DEFAULT_ATTEMPTS = 3

/** The `normal_change` tools of an agent that can be authorized ahead at all (ADR-0072 rule 1). */
export function authorizableTools(agent: Pick<SpecializedAgent, 'tools' | 'tool_classes'>): string[] {
  return agent.tools.filter((t) => agent.tool_classes[t] === 'normal_change' && !NEVER_AUTHORIZED.includes(t))
}

/**
 * The expiry the form sends for a whole number of days from `now`. The server checks
 * "at most 180 days out" against its own clock a moment later, so the ceiling is
 * pulled in by a minute rather than sent on the exact boundary.
 */
export function expiryFor(days: number, now: number = Date.now()): string {
  const ms = Math.min(days * DAY_MS, MAX_EXPIRY_DAYS * DAY_MS - 60_000)
  return new Date(now + ms).toISOString()
}

function scopeLabel(scope: string[] | string): string {
  return Array.isArray(scope) ? scope.join(', ') : scope === SERVER_SCOPE ? 'the server' : scope
}

function usedLabel(a: AgentAuthorization): string {
  const entries = Object.entries(a.attempts_last_24h ?? {})
  if (entries.length === 0) return 'none spent in the last 24 h'
  return `spent in 24 h: ${entries.map(([host, n]) => `${host} ${n}`).join(', ')}`
}

function formatDate(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

function GrantForm({ agent, tools }: { agent: SpecializedAgent; tools: string[] }) {
  const queryClient = useQueryClient()
  const fleet = useFleetHosts()
  const [tool, setTool] = useState(tools[0] ?? '')
  const [pickedKind, setKind] = useState<'hosts' | 'server'>('hosts')
  const [hosts, setHosts] = useState<string[]>([])
  const [attempts, setAttempts] = useState(String(DEFAULT_ATTEMPTS))
  const [days, setDays] = useState(String(DEFAULT_EXPIRY_DAYS))
  const [note, setNote] = useState('')
  const [granted, setGranted] = useState<string | null>(null)

  const daysNumber = Number(days)
  const daysValid = Number.isInteger(daysNumber) && daysNumber >= 1 && daysNumber <= MAX_EXPIRY_DAYS
  const attemptsNumber = Number(attempts)
  const attemptsValid = Number.isInteger(attemptsNumber) && attemptsNumber >= 1
  // Where the chosen tool runs decides which scope can mean anything: a PC tool is granted
  // on PCs, a server tool on the server. Without the map (an older server) both are offered.
  const target = agent.tool_targets?.[tool]
  const kind: 'hosts' | 'server' = target === 'server' ? 'server' : target === 'host' ? 'hosts' : pickedKind
  const scopeValid = kind === 'server' || hosts.length > 0
  const hash = agent.effective_hash

  const grant = useMutation({
    mutationFn: () =>
      api.post<AgentAuthorization>(`/api/specialized-agents/${encodeURIComponent(agent.id)}/authorizations`, {
        tool,
        scope: kind === 'server' ? SERVER_SCOPE : hosts,
        max_attempts_per_day: attemptsNumber,
        expires_at: expiryFor(daysNumber),
        note: note.trim(),
        effective_hash: hash,
      }),
    onMutate: () => setGranted(null),
    onSuccess: () => {
      setGranted(`Granted: ${tool} on ${kind === 'server' ? 'the server' : hosts.join(', ')} for ${daysNumber} day${daysNumber === 1 ? '' : 's'}.`)
      setHosts([])
      setNote('')
      queryClient.invalidateQueries({ queryKey: AGENTS_KEY })
    },
  })

  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    if (daysValid && attemptsValid && scopeValid && tool && hash) grant.mutate()
  }

  const fleetIds = fleet.data?.agents.map((a) => a.agent_id) ?? []

  return (
    <form className={styles.form} onSubmit={submit} aria-label="Grant a standing authorization">
      <div className={styles.row}>
        <label className={shared.field}>
          <span className={shared.fieldLabel}>TOOL</span>
          <select className={shared.input} value={tool} onChange={(e) => setTool(e.target.value)}>
            {tools.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label className={shared.field}>
          <span className={shared.fieldLabel}>ATTEMPTS PER HOST PER DAY</span>
          <input
            type="number"
            className={shared.input}
            min={1}
            step={1}
            value={attempts}
            onChange={(e) => setAttempts(e.target.value)}
            aria-invalid={!attemptsValid}
          />
        </label>
        <label className={shared.field}>
          <span className={shared.fieldLabel}>EXPIRES IN (DAYS, AT MOST {MAX_EXPIRY_DAYS})</span>
          <input
            type="number"
            className={shared.input}
            min={1}
            max={MAX_EXPIRY_DAYS}
            step={1}
            value={days}
            onChange={(e) => setDays(e.target.value)}
            aria-invalid={!daysValid}
            aria-describedby="grant-days-help"
          />
        </label>
      </div>
      <p id="grant-days-help" className={daysValid ? shared.help : shared.errorBox} role={daysValid ? undefined : 'alert'} style={{ marginBottom: 0 }}>
        {daysValid
          ? `Expires on ${formatDate(expiryFor(daysNumber))}.`
          : `An authorization lasts 1 to ${MAX_EXPIRY_DAYS} days. Longer needs renewing, on purpose.`}
      </p>

      <fieldset className={styles.fieldset}>
        <legend className={styles.legend}>SCOPE</legend>
        <div className={styles.checkGrid} role="radiogroup" aria-label="Scope kind">
          {target !== 'server' && (
            <label className={styles.check}>
              <input type="radio" name={`scope-${agent.id}`} checked={kind === 'hosts'} onChange={() => setKind('hosts')} />
              These PCs
            </label>
          )}
          {target !== 'host' && (
            <label className={styles.check}>
              <input type="radio" name={`scope-${agent.id}`} checked={kind === 'server'} onChange={() => setKind('server')} />
              The server (a change that touches no PC)
            </label>
          )}
        </div>
        {kind === 'hosts' && (
          <div className={styles.checkGrid} style={{ marginTop: 10 }} role="group" aria-label="PCs">
            {fleetIds.length === 0 ? (
              <span className={shared.help}>No PC is enrolled yet.</span>
            ) : (
              fleetIds.map((id) => (
                <label key={id} className={styles.check}>
                  <input
                    type="checkbox"
                    checked={hosts.includes(id)}
                    onChange={() => setHosts((h) => (h.includes(id) ? h.filter((x) => x !== id) : [...h, id]))}
                  />
                  {id}
                </label>
              ))
            )}
          </div>
        )}
        {kind === 'hosts' && (
          <p className={shared.help} style={{ marginTop: 8 }}>
            An empty selection is never &quot;all PCs&quot;: pick at least one.
          </p>
        )}
      </fieldset>

      <label className={shared.field}>
        <span className={shared.fieldLabel}>NOTE (OPTIONAL)</span>
        <input type="text" className={shared.input} value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} />
      </label>

      <div className={shared.warnBox} role="note" style={{ marginBottom: 0 }}>
        Granting lets <strong>{agent.title}</strong>, while it is in act, make this one kind of change on exactly this scope
        without asking. It is bound to the version you are looking at (hash <span className={styles.hash}>{shortHash(hash)}</span>);
        any change to the agent&apos;s spec or parameters voids it for good.
      </div>

      <div className={shared.actions} style={{ marginTop: 0 }}>
        <button type="submit" className={shared.btnPrimary} disabled={grant.isPending || !daysValid || !attemptsValid || !scopeValid || !tool || !hash}>
          {grant.isPending ? 'GRANTING…' : 'GRANT AUTHORIZATION'}
        </button>
      </div>

      <div aria-live="polite">
        {granted && <div className={shared.okBox}>{granted}</div>}
        {grant.isError && (
          <div className={shared.errorBox} role="alert">
            {consentErrorMessage(grant.error, 'Could not grant the authorization.')}
            {isHashChanged(grant.error) && (
              <>
                {' '}
                <button
                  type="button"
                  className={styles.linkButton}
                  onClick={() => {
                    grant.reset()
                    queryClient.invalidateQueries({ queryKey: AGENTS_KEY })
                  }}
                >
                  RELOAD
                </button>
              </>
            )}
          </div>
        )}
      </div>
    </form>
  )
}

export interface AuthorizationsPanelProps {
  agent: SpecializedAgent
  canManage: boolean
}

/** Standing authorizations (ADR-0072): consent given ahead for one kind of `normal_change`. */
export default function AuthorizationsPanel({ agent, canManage }: AuthorizationsPanelProps) {
  const queryClient = useQueryClient()
  const query = useAgentAuthorizations(agent.id)
  const tools = authorizableTools(agent)
  const headingId = `auths-${agent.id}`

  const revoke = useMutation({
    mutationFn: (id: string) =>
      api.delete<AgentAuthorization>(
        `/api/specialized-agents/${encodeURIComponent(agent.id)}/authorizations/${encodeURIComponent(id)}`,
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: AGENTS_KEY }),
  })

  function onRevoke(a: AgentAuthorization) {
    if (!window.confirm(`Revoke the authorization for ${a.tool} on ${scopeLabel(a.scope)}? The next call it would have covered is refused.`)) return
    revoke.mutate(a.id)
  }

  let table
  if (query.isLoading) table = <div className={shared.loading}>Loading…</div>
  else if (query.isError || !query.data) {
    table = <EmptyState title="Could not load the authorizations" message="Something went wrong. Reload to try again." />
  } else if (query.data.authorizations.length === 0) {
    table = <p className={shared.help}>No standing authorizations. Without one, every normal change this agent proposes is kept as a recommendation.</p>
  } else {
    table = (
      <div className={styles.scroller} role="region" aria-label="Standing authorizations" tabIndex={0}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th scope="col">TOOL</th>
              <th scope="col">SCOPE</th>
              <th scope="col">ATTEMPTS / DAY</th>
              <th scope="col">EXPIRES</th>
              <th scope="col">GRANTED BY</th>
              <th scope="col">STATE</th>
              {canManage && <th scope="col" aria-label="Actions" />}
            </tr>
          </thead>
          <tbody>
            {query.data.authorizations.map((a) => (
              <tr key={a.id}>
                <td className={styles.cellBreak}>
                  <span className={styles.toolName}>{a.tool}</span>
                  {a.note && <div className={shared.tableSub}>{a.note}</div>}
                </td>
                <td className={styles.cellBreak}>{scopeLabel(a.scope)}</td>
                <td className={styles.cellBreak}>
                  {a.max_attempts_per_day}
                  <div className={shared.tableSub}>{usedLabel(a)}</div>
                </td>
                <td>{formatDate(a.expires_at)}</td>
                <td className={styles.cellBreak}>{a.granted_by}</td>
                <td>
                  <AuthorizationStateTag status={a.status} />
                </td>
                {canManage && (
                  <td>
                    {a.status === 'live' && (
                      <button
                        type="button"
                        className={shared.btnDanger}
                        onClick={() => onRevoke(a)}
                        disabled={revoke.isPending}
                        aria-label={`Revoke ${a.tool} on ${scopeLabel(a.scope)}`}
                      >
                        REVOKE
                      </button>
                    )}
                  </td>
                )}
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
        STANDING AUTHORIZATIONS
      </h3>
      <p className={shared.help} style={{ marginBottom: 12 }}>
        A <strong>normal change</strong> only runs when a live authorization covers it: one tool, named PCs, an attempt budget
        per PC per day, an expiry. {NEVER_AUTHORIZED.join(', ')} can never be authorized.
      </p>
      {revoke.isError && (
        <div className={shared.errorBox} role="alert">
          {consentErrorMessage(revoke.error, 'Could not revoke the authorization.')}
        </div>
      )}
      {table}

      {canManage ? (
        <div style={{ marginTop: 20 }}>
          <h4 className={styles.sub}>GRANT ONE</h4>
          {tools.length === 0 ? (
            <p className={shared.help}>This agent names no normal-change tool that can be authorized, so there is nothing to grant.</p>
          ) : (
            <GrantForm agent={agent} tools={tools} />
          )}
        </div>
      ) : (
        <p className={shared.help} style={{ marginTop: 12 }}>
          Granting and revoking need a superuser signed in to the dashboard.
        </p>
      )}
    </section>
  )
}

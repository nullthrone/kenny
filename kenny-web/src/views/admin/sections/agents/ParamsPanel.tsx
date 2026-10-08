import { useState, type FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../../../../api/client'
import EmptyState from '../../../../components/EmptyState/EmptyState'
import type { AgentParamsSaved, AgentParamValues, AgentWindow, SpecializedAgent } from '../../types'
import { AGENTS_KEY, consentErrorMessage, isHashChanged, useAgentParams, useFleetHosts } from './queries'
import shared from '../../shared.module.css'
import styles from './agents.module.css'

const DAYS: { key: string; label: string }[] = [
  { key: 'mon', label: 'Mon' },
  { key: 'tue', label: 'Tue' },
  { key: 'wed', label: 'Wed' },
  { key: 'thu', label: 'Thu' },
  { key: 'fri', label: 'Fri' },
  { key: 'sat', label: 'Sat' },
  { key: 'sun', label: 'Sun' },
]

const PARAM_LABEL: Record<string, string> = {
  window: 'Maintenance window',
  hosts: 'Hosts',
  packages: 'Allowed package ids',
  require_idle: 'Only when nobody is signed in',
}

function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

function splitList(text: string): string[] {
  return [...new Set(text.split(/[\n,]/).map((v) => v.trim()).filter(Boolean))].sort()
}

/** The values as the person reads them: the window as a sentence, a list joined, a flag as yes/no. */
function ReadOnlyValue({ name, values }: { name: string; values: AgentParamValues }) {
  const value = values[name]
  if (name === 'window') {
    const w = value as AgentWindow | undefined
    if (!w || !w.days?.length) return <>not set</>
    return (
      <>
        {w.days.join(', ')} · {w.start}–{w.end} · {w.tz}
      </>
    )
  }
  if (name === 'require_idle') return <>{value === false ? 'no' : 'yes'}</>
  if (Array.isArray(value)) return <>{value.length === 0 ? 'none' : value.join(', ')}</>
  return <>not set</>
}

interface FormProps {
  agent: SpecializedAgent
  declared: string[]
  initial: AgentParamValues
}

function ParamsForm({ agent, declared, initial }: FormProps) {
  const queryClient = useQueryClient()
  const fleet = useFleetHosts()

  const [days, setDays] = useState<string[]>(initial.window?.days ?? [])
  const [start, setStart] = useState(initial.window?.start ?? '02:00')
  const [end, setEnd] = useState(initial.window?.end ?? '05:00')
  const [tz, setTz] = useState(initial.window?.tz ?? browserTimezone())
  const [hosts, setHosts] = useState<string[]>(Array.isArray(initial.hosts) ? initial.hosts : [])
  const [listText, setListText] = useState<Record<string, string>>(() => {
    const out: Record<string, string> = {}
    for (const name of declared) {
      const v = initial[name]
      if (name !== 'window' && name !== 'require_idle' && name !== 'hosts') {
        out[name] = Array.isArray(v) ? (v as string[]).join('\n') : ''
      }
    }
    return out
  })
  // An absent flag means true on the server: a host with somebody signed in is skipped.
  const [requireIdle, setRequireIdle] = useState(initial.require_idle !== false)
  const [saved, setSaved] = useState<string | null>(null)

  const save = useMutation({
    mutationFn: (params: AgentParamValues) =>
      api.put<AgentParamsSaved>(`/api/specialized-agents/${encodeURIComponent(agent.id)}/params`, { params }),
    onMutate: () => setSaved(null),
    onSuccess: (res) => {
      const bits = [`Saved. The agent is in ${res.mode}.`]
      if (res.voided > 0) bits.push(`${res.voided} standing authorization${res.voided === 1 ? '' : 's'} voided.`)
      setSaved(bits.join(' '))
      queryClient.invalidateQueries({ queryKey: AGENTS_KEY })
    },
  })

  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const params: AgentParamValues = {}
    for (const name of declared) {
      if (name === 'window') {
        if (days.length > 0) params.window = { days, start, end, tz: tz.trim() || browserTimezone() }
      } else if (name === 'require_idle') {
        params.require_idle = requireIdle
      } else if (name === 'hosts') {
        params.hosts = hosts
      } else {
        params[name] = splitList(listText[name] ?? '')
      }
    }
    save.mutate(params)
  }

  function toggleDay(key: string) {
    setDays((d) => (d.includes(key) ? d.filter((x) => x !== key) : [...d, key]))
  }

  function toggleHost(id: string) {
    setHosts((h) => (h.includes(id) ? h.filter((x) => x !== id) : [...h, id]))
  }

  const fleetIds = fleet.data?.agents.map((a) => a.agent_id) ?? []
  const selectedHosts = hosts
  // A saved host that has left the fleet stays visible, so saving does not drop it silently.
  const hostChoices = [...fleetIds, ...selectedHosts.filter((h) => !fleetIds.includes(h))]

  return (
    <form className={styles.form} onSubmit={submit} aria-label={`Parameters of ${agent.title}`}>
      {declared.includes('window') && (
        <fieldset className={styles.fieldset}>
          <legend className={styles.legend}>{PARAM_LABEL.window.toUpperCase()}</legend>
          <div className={styles.checkGrid} role="group" aria-label="Days">
            {DAYS.map((d) => (
              <label key={d.key} className={styles.check}>
                <input type="checkbox" checked={days.includes(d.key)} onChange={() => toggleDay(d.key)} />
                {d.label}
              </label>
            ))}
          </div>
          <div className={styles.row} style={{ marginTop: 10 }}>
            <label className={shared.field}>
              <span className={shared.fieldLabel}>START</span>
              <input type="time" className={shared.input} value={start} onChange={(e) => setStart(e.target.value)} required />
            </label>
            <label className={shared.field}>
              <span className={shared.fieldLabel}>END</span>
              <input type="time" className={shared.input} value={end} onChange={(e) => setEnd(e.target.value)} required />
            </label>
            <label className={shared.field}>
              <span className={shared.fieldLabel}>TIME ZONE</span>
              <input
                type="text"
                className={shared.input}
                value={tz}
                placeholder="Europe/Berlin"
                onChange={(e) => setTz(e.target.value)}
              />
            </label>
          </div>
          <p className={shared.help} style={{ marginTop: 8 }}>
            No day ticked means no window: the agent does not start on a schedule. An end before the start runs past midnight.
          </p>
        </fieldset>
      )}

      {declared.includes('hosts') && (
        <fieldset className={styles.fieldset}>
          <legend className={styles.legend}>{PARAM_LABEL.hosts.toUpperCase()}</legend>
          {hostChoices.length === 0 ? (
            <p className={shared.help}>No PC is enrolled yet.</p>
          ) : (
            <div className={styles.checkGrid}>
              {hostChoices.map((id) => (
                <label key={id} className={styles.check}>
                  <input type="checkbox" checked={selectedHosts.includes(id)} onChange={() => toggleHost(id)} />
                  {id}
                  {!fleetIds.includes(id) && fleet.data ? ' (not in the fleet)' : ''}
                </label>
              ))}
            </div>
          )}
          <p className={shared.help} style={{ marginTop: 8 }}>
            The agent only ever looks at these PCs, one at a time. An empty selection means none, never all.
          </p>
        </fieldset>
      )}

      {declared
        .filter((n) => n !== 'window' && n !== 'hosts' && n !== 'require_idle')
        .map((name) => (
          <div key={name} className={shared.field}>
            <label className={shared.fieldLabel} htmlFor={`param-${agent.id}-${name}`}>
              {(PARAM_LABEL[name] ?? name).toUpperCase()}
            </label>
            <textarea
              id={`param-${agent.id}-${name}`}
              className={styles.textarea}
              value={listText[name] ?? ''}
              onChange={(e) => setListText((t) => ({ ...t, [name]: e.target.value }))}
              spellCheck={false}
              placeholder="one per line"
              aria-describedby={`param-${agent.id}-${name}-help`}
            />
            <span id={`param-${agent.id}-${name}-help`} className={shared.help}>
              One exact value per line. An empty list admits nothing, never everything.
            </span>
          </div>
        ))}

      {declared.includes('require_idle') && (
        <label className={styles.check}>
          <input type="checkbox" checked={requireIdle} onChange={(e) => setRequireIdle(e.target.checked)} />
          <span>
            <strong>{PARAM_LABEL.require_idle}</strong> — a PC with somebody signed in is skipped
          </span>
        </label>
      )}

      <div className={shared.warnBox} role="note" style={{ marginBottom: 0 }}>
        Saving a change drops this agent back to <strong>shadow</strong> and voids its standing authorizations for good.
        A superuser has to choose act again afterwards, for what then stands on this page. Saving the values it already
        has changes nothing.
      </div>

      <div className={shared.actions} style={{ marginTop: 0 }}>
        <button type="submit" className={shared.btnPrimary} disabled={save.isPending}>
          {save.isPending ? 'SAVING…' : 'SAVE PARAMETERS'}
        </button>
      </div>

      <div aria-live="polite">
        {saved && <div className={shared.okBox}>{saved}</div>}
        {save.isError && (
          <div className={shared.errorBox} role="alert">
            {consentErrorMessage(save.error, 'Could not save the parameters.')}
            {isHashChanged(save.error) && (
              <>
                {' '}
                <button
                  type="button"
                  className={styles.linkButton}
                  onClick={() => {
                    save.reset()
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

export interface ParamsPanelProps {
  agent: SpecializedAgent
  canManage: boolean
}

/** The parameters an install sets without a code change (ADR-0072): a window, the hosts, an allowlist. */
export default function ParamsPanel({ agent, canManage }: ParamsPanelProps) {
  const query = useAgentParams(agent.id)
  const headingId = `params-${agent.id}`

  let body
  if (query.isLoading) body = <div className={shared.loading}>Loading…</div>
  else if (query.isError || !query.data) {
    body = <EmptyState title="Could not load the parameters" message="Something went wrong. Reload to try again." />
  } else if (query.data.declared.length === 0) {
    body = <p className={shared.help}>This agent takes no parameters.</p>
  } else if (canManage) {
    // Re-keyed on what the server holds, so a reload or a save shows the stored values, not a stale draft.
    body = (
      <ParamsForm
        key={`${query.data.effective_hash}:${JSON.stringify(query.data.params)}`}
        agent={agent}
        declared={query.data.declared}
        initial={query.data.params}
      />
    )
  } else {
    body = (
      <>
        <dl className={styles.dl}>
          {query.data.declared.map((name) => (
            <div key={name} style={{ display: 'contents' }}>
              <dt>{(PARAM_LABEL[name] ?? name).toUpperCase()}</dt>
              <dd className="kc-evidence">
                <ReadOnlyValue name={name} values={query.data.params} />
              </dd>
            </div>
          ))}
        </dl>
        <p className={shared.help} style={{ marginTop: 10 }}>
          Editing parameters needs a superuser signed in to the dashboard.
        </p>
      </>
    )
  }

  return (
    <section className={shared.card} aria-labelledby={headingId}>
      <h3 className={styles.heading} id={headingId}>
        PARAMETERS
      </h3>
      {body}
    </section>
  )
}

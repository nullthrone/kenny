import { useState, type FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../../../../api/client'
import EmptyState from '../../../../components/EmptyState/EmptyState'
import type { Steward, StewardParamKind, StewardParamsSaved, StewardParamValues, StewardWindow } from '../../types'
import { STEWARDS_KEY, consentErrorMessage, isHashChanged, useStewardParams, useFleetHosts } from './queries'
import shared from '../../shared.module.css'
import styles from './stewards.module.css'

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
  min_percent_used: 'Minimum disk usage (%)',
  targets: 'Cleanup targets',
}

const PARAM_HINT: Record<string, string> = {
  require_idle: 'a PC with somebody signed in is skipped',
}

/** The host picker replaces the textarea for this list parameter; its values are still a list. */
const HOSTS_PARAM = 'hosts'

const PERCENT_MIN = 1
const PERCENT_MAX = 99
const PERCENT_ERROR = `must be a whole number from ${PERCENT_MIN} to ${PERCENT_MAX}`

/** A parameter's kind; a name the server did not classify is a list, as on the server. */
function kindOf(kinds: Record<string, StewardParamKind>, name: string): StewardParamKind {
  return kinds[name] ?? 'list'
}

function labelOf(name: string): string {
  return PARAM_LABEL[name] ?? name
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

/** The values as the person reads them: the window as a sentence, a list joined, a flag as yes/no, a percent with its sign. */
function ReadOnlyValue({ kind, value }: { kind: StewardParamKind; value: StewardParamValues[string] }) {
  if (kind === 'window') {
    const w = value as StewardWindow | undefined
    if (!w || !w.days?.length) return <>not set</>
    return (
      <>
        {w.days.join(', ')} · {w.start}–{w.end} · {w.tz}
      </>
    )
  }
  if (kind === 'bool') return <>{value === false ? 'no' : 'yes'}</>
  if (kind === 'percent') return <>{typeof value === 'number' ? `${value} %` : 'not set'}</>
  if (Array.isArray(value)) return <>{value.length === 0 ? 'none' : value.join(', ')}</>
  return <>not set</>
}

interface FormProps {
  steward: Steward
  declared: string[]
  kinds: Record<string, StewardParamKind>
  initial: StewardParamValues
}

/** `text` as a percent: `undefined` when empty (unset), the number when 1-99, else `'invalid'`. */
function parsePercent(text: string): number | undefined | 'invalid' {
  const t = text.trim()
  if (t === '') return undefined
  if (!/^\d+$/.test(t)) return 'invalid'
  const n = Number(t)
  return n >= PERCENT_MIN && n <= PERCENT_MAX ? n : 'invalid'
}

function ParamsForm({ steward, declared, kinds, initial }: FormProps) {
  const queryClient = useQueryClient()
  const fleet = useFleetHosts()

  const names = (kind: StewardParamKind) => declared.filter((n) => kindOf(kinds, n) === kind)
  const windowName = names('window')[0]
  const initialWindow = windowName ? (initial[windowName] as StewardWindow | undefined) : undefined
  const listValue = (name: string) => (Array.isArray(initial[name]) ? (initial[name] as string[]) : [])

  const [days, setDays] = useState<string[]>(initialWindow?.days ?? [])
  const [start, setStart] = useState(initialWindow?.start ?? '02:00')
  const [end, setEnd] = useState(initialWindow?.end ?? '05:00')
  const [tz, setTz] = useState(initialWindow?.tz ?? browserTimezone())
  const [hosts, setHosts] = useState<string[]>(listValue(HOSTS_PARAM))
  const [listText, setListText] = useState<Record<string, string>>(() =>
    Object.fromEntries(names('list').filter((n) => n !== HOSTS_PARAM).map((n) => [n, listValue(n).join('\n')])),
  )
  const [percentText, setPercentText] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      names('percent').map((n) => [n, typeof initial[n] === 'number' ? String(initial[n]) : '']),
    ),
  )
  // An absent flag means true on the server: a host with somebody signed in is skipped.
  const [flags, setFlags] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(names('bool').map((n) => [n, initial[n] !== false])),
  )
  const [formError, setFormError] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)

  const save = useMutation({
    mutationFn: (params: StewardParamValues) =>
      api.put<StewardParamsSaved>(`/api/stewards/${encodeURIComponent(steward.id)}/params`, { params }),
    onMutate: () => setSaved(null),
    onSuccess: (res) => {
      const bits = [`Saved. The steward is in ${res.mode}.`]
      if (res.voided > 0) bits.push(`${res.voided} standing authorization${res.voided === 1 ? '' : 's'} voided.`)
      setSaved(bits.join(' '))
      queryClient.invalidateQueries({ queryKey: STEWARDS_KEY })
    },
  })

  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setFormError(null)
    const params: StewardParamValues = {}
    for (const name of declared) {
      const kind = kindOf(kinds, name)
      if (kind === 'window') {
        if (days.length > 0) params[name] = { days, start, end, tz: tz.trim() || browserTimezone() }
      } else if (kind === 'bool') {
        params[name] = flags[name] ?? true
      } else if (kind === 'percent') {
        const n = parsePercent(percentText[name] ?? '')
        if (n === 'invalid') {
          setFormError(`${labelOf(name)} ${PERCENT_ERROR}, or leave it empty to use the default.`)
          return
        }
        // Unset is an absent key: the server rejects null, and a steward falls back to its default.
        if (n !== undefined) params[name] = n
      } else if (name === HOSTS_PARAM) {
        params[name] = hosts
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
    <form className={styles.form} onSubmit={submit} aria-label={`Parameters of ${steward.title}`}>
      {windowName !== undefined && (
        <fieldset className={styles.fieldset}>
          <legend className={styles.legend}>{labelOf(windowName).toUpperCase()}</legend>
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
            No day ticked means no window: the steward does not start on a schedule. An end before the start runs past midnight.
          </p>
        </fieldset>
      )}

      {names('list').includes(HOSTS_PARAM) && (
        <fieldset className={styles.fieldset}>
          <legend className={styles.legend}>{labelOf(HOSTS_PARAM).toUpperCase()}</legend>
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
            The steward only ever looks at these PCs, one at a time. An empty selection means none, never all.
          </p>
        </fieldset>
      )}

      {declared
        .filter((n) => kindOf(kinds, n) === 'percent')
        .map((name) => (
          <div key={name} className={shared.field}>
            <label className={shared.fieldLabel} htmlFor={`param-${steward.id}-${name}`}>
              {labelOf(name).toUpperCase()}
            </label>
            <input
              id={`param-${steward.id}-${name}`}
              type="number"
              inputMode="numeric"
              className={shared.input}
              style={{ maxWidth: 160 }}
              // No min/max/step: the browser would block the submit with its own tooltip, and the
              // form states the range itself (parsePercent).
              step="any"
              value={percentText[name] ?? ''}
              onChange={(e) => setPercentText((t) => ({ ...t, [name]: e.target.value }))}
              aria-describedby={`param-${steward.id}-${name}-help`}
            />
            <span id={`param-${steward.id}-${name}-help`} className={shared.help}>
              A whole number from {PERCENT_MIN} to {PERCENT_MAX}. Leave it empty to use the steward&apos;s default.
            </span>
          </div>
        ))}

      {declared
        .filter((n) => kindOf(kinds, n) === 'list' && n !== HOSTS_PARAM)
        .map((name) => (
          <div key={name} className={shared.field}>
            <label className={shared.fieldLabel} htmlFor={`param-${steward.id}-${name}`}>
              {labelOf(name).toUpperCase()}
            </label>
            <textarea
              id={`param-${steward.id}-${name}`}
              className={styles.textarea}
              value={listText[name] ?? ''}
              onChange={(e) => setListText((t) => ({ ...t, [name]: e.target.value }))}
              spellCheck={false}
              placeholder="one per line"
              aria-describedby={`param-${steward.id}-${name}-help`}
            />
            <span id={`param-${steward.id}-${name}-help`} className={shared.help}>
              One exact value per line. An empty list admits nothing, never everything.
            </span>
          </div>
        ))}

      {declared
        .filter((n) => kindOf(kinds, n) === 'bool')
        .map((name) => (
          <label key={name} className={styles.check}>
            <input
              type="checkbox"
              checked={flags[name] ?? true}
              onChange={(e) => setFlags((f) => ({ ...f, [name]: e.target.checked }))}
            />
            <span>
              <strong>{labelOf(name)}</strong>
              {PARAM_HINT[name] ? ` — ${PARAM_HINT[name]}` : ''}
            </span>
          </label>
        ))}

      <div className={shared.warnBox} role="note" style={{ marginBottom: 0 }}>
        Saving a change drops this steward back to <strong>shadow</strong> and voids its standing authorizations for good.
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
        {formError && (
          <div className={shared.errorBox} role="alert">
            {formError}
          </div>
        )}
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
                    queryClient.invalidateQueries({ queryKey: STEWARDS_KEY })
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
  steward: Steward
  canManage: boolean
}

/** The parameters an install sets without a code change (ADR-0072): a window, the hosts, an allowlist. */
export default function ParamsPanel({ steward, canManage }: ParamsPanelProps) {
  const query = useStewardParams(steward.id)
  const headingId = `params-${steward.id}`

  let body
  if (query.isLoading) body = <div className={shared.loading}>Loading…</div>
  else if (query.isError || !query.data) {
    body = <EmptyState title="Could not load the parameters" message="Something went wrong. Reload to try again." />
  } else if (query.data.declared.length === 0) {
    body = <p className={shared.help}>This steward takes no parameters.</p>
  } else if (canManage) {
    // Re-keyed on what the server holds, so a reload or a save shows the stored values, not a stale draft.
    body = (
      <ParamsForm
        key={`${query.data.effective_hash}:${JSON.stringify(query.data.params)}`}
        steward={steward}
        declared={query.data.declared}
        kinds={query.data.param_kinds}
        initial={query.data.params}
      />
    )
  } else {
    body = (
      <>
        <dl className={styles.dl}>
          {query.data.declared.map((name) => (
            <div key={name} style={{ display: 'contents' }}>
              <dt>{labelOf(name).toUpperCase()}</dt>
              <dd className="kc-evidence">
                <ReadOnlyValue kind={kindOf(query.data.param_kinds, name)} value={query.data.params[name]} />
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

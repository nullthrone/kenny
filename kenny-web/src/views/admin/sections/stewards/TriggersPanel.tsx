import { useState, type FormEvent } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import EmptyState from '../../../../components/EmptyState/EmptyState'
import type { Steward, StewardTriggerConfig, StewardTriggersAllowed, StewardTriggersResponse } from '../../types'
import { STEWARDS_KEY, consentErrorMessage, isHashChanged, useSaveStewardTriggers, useStewardTriggers } from './queries'
import shared from '../../shared.module.css'
import styles from './stewards.module.css'

const DEFAULT_COOLDOWN_HOURS = 24

const EVENT_LABEL: Record<string, string> = {
  ticket_created: 'New ticket',
  'alert:offline': 'Offline alert',
  'alert:disk_forecast': 'Disk forecast alert',
  'alert:hardware_forecast': 'Hardware forecast alert',
  'alert:change': 'Change alert',
}

/** `alert:health:disk` reads "Health alert: disk"; a name nobody labelled reads as it is. */
export function eventLabel(name: string): string {
  const known = EVENT_LABEL[name]
  if (known) return known
  const health = /^alert:health:(.+)$/.exec(name)
  if (health) return `Health alert: ${health[1].replace(/_/g, ' ')}`
  return name
}

/** The canonical form the server compares: events sorted by name, so equal means the same thing. */
function canonical(config: StewardTriggerConfig): string {
  return JSON.stringify({
    schedule: config.schedule === null ? null : { min_interval_days: config.schedule.min_interval_days },
    events: [...config.events]
      .map((e) => ({ name: e.name, cooldown_hours: e.cooldown_hours }))
      .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0)),
    command: config.command,
  })
}

/** A whole number in `[low, high]`, else `null`. */
function whole(text: string, low: number, high: number): number | null {
  if (!/^\d+$/.test(text.trim())) return null
  const n = Number(text)
  return n >= low && n <= high ? n : null
}

interface Draft {
  schedule: boolean
  /** Empty means no minimum: every window the schedule is open. */
  intervalDays: string
  /** Event name -> cooldown hours as typed; present means switched on. */
  events: Record<string, string>
  command: boolean
}

function toDraft(config: StewardTriggerConfig): Draft {
  return {
    schedule: config.schedule !== null,
    intervalDays: config.schedule?.min_interval_days != null ? String(config.schedule.min_interval_days) : '',
    events: Object.fromEntries(config.events.map((e) => [e.name, String(e.cooldown_hours)])),
    command: config.command,
  }
}

/** The draft as the wire shape, restricted to what the spec allows; `error` when a number is out of range. */
function fromDraft(draft: Draft, allowed: StewardTriggersAllowed): { config: StewardTriggerConfig } | { error: string } {
  let schedule: StewardTriggerConfig['schedule'] = null
  if (allowed.schedule && draft.schedule) {
    if (draft.intervalDays.trim() === '') {
      schedule = { min_interval_days: null }
    } else {
      const days = whole(draft.intervalDays, 1, 366)
      if (days === null) return { error: 'The interval is a whole number of days from 1 to 366, or empty.' }
      schedule = { min_interval_days: days }
    }
  }
  const events: StewardTriggerConfig['events'] = []
  for (const name of allowed.events) {
    const text = draft.events[name]
    if (text === undefined) continue
    const hours = whole(text, 1, 168)
    if (hours === null) return { error: `The cooldown for ${eventLabel(name)} is a whole number of hours from 1 to 168.` }
    events.push({ name, cooldown_hours: hours })
  }
  return { config: { schedule, events, command: allowed.command && draft.command } }
}

/** What is switched on, as sentences: the read-only view of a configuration. */
function Summary({ config }: { config: StewardTriggerConfig }) {
  const items: string[] = []
  if (config.schedule) {
    const d = config.schedule.min_interval_days
    items.push(d === null ? 'On a schedule, in its maintenance window' : `On a schedule, at most every ${d} day${d === 1 ? '' : 's'}`)
  }
  for (const e of config.events) {
    items.push(`${eventLabel(e.name)} (at most once every ${e.cooldown_hours} h per subject)`)
  }
  if (config.command) items.push('Run on command')
  if (items.length === 0) return <p className={shared.help}>Nothing starts this steward on its own.</p>
  return (
    <ul className={styles.constraintList} aria-label="What starts this steward">
      {items.map((i) => (
        <li key={i}>{i}</li>
      ))}
    </ul>
  )
}

interface FormProps {
  steward: Steward
  data: StewardTriggersResponse
}

function TriggersForm({ steward, data }: FormProps) {
  const queryClient = useQueryClient()
  const save = useSaveStewardTriggers(steward.id)
  const { allowed, baseline } = data
  const [draft, setDraft] = useState<Draft>(() => toDraft(data.triggers))
  const [problem, setProblem] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)

  const parsed = fromDraft(draft, allowed)
  const differs = 'config' in parsed ? canonical(parsed.config) !== canonical(baseline) : true
  const hasChoices = allowed.schedule || allowed.events.length > 0 || allowed.command

  function patch(change: Partial<Draft>) {
    setDraft((d) => ({ ...d, ...change }))
    setProblem(null)
    setSaved(null)
    save.reset()
  }

  function toggleEvent(name: string, on: boolean) {
    const events = { ...draft.events }
    if (on) events[name] = events[name] ?? String(baseline.events.find((e) => e.name === name)?.cooldown_hours ?? DEFAULT_COOLDOWN_HOURS)
    else delete events[name]
    patch({ events })
  }

  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const result = fromDraft(draft, allowed)
    if ('error' in result) {
      setProblem(result.error)
      return
    }
    setProblem(null)
    setSaved(null)
    // The hash the page showed: the server refuses with a 409 when the steward has moved on.
    save.mutate(
      { triggers: result.config, effective_hash: data.effective_hash },
      {
        onSuccess: (res) => {
          const bits = [`Saved. The steward is in ${res.mode}.`]
          if (res.voided > 0) bits.push(`${res.voided} standing authorization${res.voided === 1 ? '' : 's'} voided.`)
          setSaved(bits.join(' '))
        },
      },
    )
  }

  return (
    <form className={styles.form} onSubmit={submit} noValidate aria-label={`Triggers of ${steward.title}`}>
      {!hasChoices && <p className={shared.help}>The spec of this steward offers nothing to switch on.</p>}

      {allowed.schedule && (
        <fieldset className={styles.fieldset}>
          <legend className={styles.legend}>SCHEDULE</legend>
          <label className={styles.check}>
            <input type="checkbox" checked={draft.schedule} onChange={(e) => patch({ schedule: e.target.checked })} />
            <strong>Run on a schedule</strong>
          </label>
          {draft.schedule && (
            <div className={styles.row} style={{ marginTop: 10 }}>
              <label className={shared.field}>
                <span className={shared.fieldLabel}>EVERY N DAYS (OPTIONAL)</span>
                <input
                  type="number"
                  className={shared.input}
                  min={1}
                  max={366}
                  step={1}
                  value={draft.intervalDays}
                  placeholder="every window"
                  onChange={(e) => patch({ intervalDays: e.target.value })}
                />
              </label>
            </div>
          )}
          <p className={shared.help} style={{ marginTop: 8 }}>
            The days and hours it may start in are the maintenance window, set in the parameters above. An interval makes it wait
            at least that many days between runs; empty means every window.
          </p>
        </fieldset>
      )}

      {allowed.events.length > 0 && (
        <fieldset className={styles.fieldset}>
          <legend className={styles.legend}>EVENTS</legend>
          <div className={styles.eventList}>
            {allowed.events.map((name) => {
              const on = draft.events[name] !== undefined
              const label = eventLabel(name)
              return (
                <div key={name} className={styles.eventRow}>
                  <label className={styles.check}>
                    <input type="checkbox" checked={on} onChange={(e) => toggleEvent(name, e.target.checked)} />
                    {label}
                  </label>
                  {on && (
                    <label className={styles.cooldown}>
                      <span className={shared.fieldLabel}>COOLDOWN (HOURS)</span>
                      <input
                        type="number"
                        className={shared.input}
                        aria-label={`Cooldown hours for ${label}`}
                        min={1}
                        max={168}
                        step={1}
                        value={draft.events[name]}
                        onChange={(e) => patch({ events: { ...draft.events, [name]: e.target.value } })}
                      />
                    </label>
                  )}
                </div>
              )
            })}
          </div>
          <p className={shared.help} style={{ marginTop: 8 }}>
            A run starts when one of these happens. The cooldown is how long the same subject (a PC or a ticket) is left alone
            after a run, from 1 to 168 hours.
          </p>
        </fieldset>
      )}

      {allowed.command && (
        <fieldset className={styles.fieldset}>
          <legend className={styles.legend}>COMMAND</legend>
          <label className={styles.check}>
            <input type="checkbox" checked={draft.command} onChange={(e) => patch({ command: e.target.checked })} />
            <strong>Run on command</strong>
          </label>
          <p className={shared.help} style={{ marginTop: 8 }}>
            A superuser can start a run from Ask kenny or here; operators can start previews, which never change anything.
          </p>
        </fieldset>
      )}

      <div className={styles.row}>
        {differs ? <span className={`${styles.tag} ${styles.toneBrass}`}>DIFFERS FROM DEFAULT</span> : <span className={shared.help}>This is the default.</span>}
        <button type="button" className={shared.btn} disabled={!differs} onClick={() => setDraft(toDraft(baseline))}>
          RESET TO DEFAULT
        </button>
      </div>

      <div className={shared.warnBox} role="note" style={{ marginBottom: 0 }}>
        Saving a change moves this steward to <strong>shadow</strong> and voids its standing authorizations for good. A superuser
        has to choose act again afterwards, for what then stands on this page. Saving what is already in force changes nothing.
      </div>

      <div className={shared.actions} style={{ marginTop: 0 }}>
        <button type="submit" className={shared.btnPrimary} disabled={save.isPending}>
          {save.isPending ? 'SAVING…' : 'SAVE TRIGGERS'}
        </button>
      </div>

      <div aria-live="polite">
        {saved && <div className={shared.okBox}>{saved}</div>}
        {problem && (
          <div className={shared.errorBox} role="alert">
            {problem}
          </div>
        )}
        {save.isError && (
          <div className={shared.errorBox} role="alert">
            {consentErrorMessage(save.error, 'Could not save the triggers.')}
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

export interface TriggersPanelProps {
  steward: Steward
  canManage: boolean
}

/** What may start a steward: a schedule, events, a command. A person picks within the ceiling its spec sets. */
export default function TriggersPanel({ steward, canManage }: TriggersPanelProps) {
  const query = useStewardTriggers(steward.id)
  const headingId = `triggers-${steward.id}`

  let body
  if (query.isLoading) body = <div className={shared.loading}>Loading…</div>
  else if (query.isError || !query.data) {
    body = <EmptyState title="Could not load the triggers" message="Something went wrong. Reload to try again." />
  } else if (!query.data.allowed.editable) {
    body = (
      <>
        <Summary config={query.data.triggers} />
        <p className={shared.help} style={{ marginTop: 10 }}>
          The triggers of this steward are fixed by its spec and cannot be changed here.
        </p>
      </>
    )
  } else if (canManage) {
    // Re-keyed on what the server holds, so a reload or a save shows the stored values, not a stale draft.
    body = (
      <TriggersForm
        key={`${query.data.effective_hash}:${canonical(query.data.triggers)}`}
        steward={steward}
        data={query.data}
      />
    )
  } else {
    body = (
      <>
        <Summary config={query.data.triggers} />
        <p className={shared.help} style={{ marginTop: 10 }}>
          Editing triggers needs a superuser signed in to the dashboard.
        </p>
      </>
    )
  }

  return (
    <section className={shared.card} aria-labelledby={headingId}>
      <h3 className={styles.heading} id={headingId}>
        TRIGGERS
      </h3>
      {body}
    </section>
  )
}

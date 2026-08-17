import { useState, type FormEvent } from 'react'
import type { WebfilterCategory, WebfilterScheduleState, WebfilterScheduleWindow } from '../types'
import { useAddWebfilterWindow, useRemoveWebfilterWindow } from '../api'
import styles from './WebFilterSchedule.module.css'

export interface WebFilterScheduleProps {
  agentId: string
  schedule: WebfilterScheduleState
  categories: WebfilterCategory[]
}

const DAY_OPTIONS: { key: string; label: string }[] = [
  { key: 'mon', label: 'Mon' },
  { key: 'tue', label: 'Tue' },
  { key: 'wed', label: 'Wed' },
  { key: 'thu', label: 'Thu' },
  { key: 'fri', label: 'Fri' },
  { key: 'sat', label: 'Sat' },
  { key: 'sun', label: 'Sun' },
]

function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

function labelFor(categories: WebfilterCategory[], key: string): string {
  return categories.find((c) => c.key === key)?.label ?? key
}

function WindowRow({
  window,
  categories,
  active,
  onRemove,
  removing,
}: {
  window: WebfilterScheduleWindow
  categories: WebfilterCategory[]
  active: boolean
  onRemove: () => void
  removing: boolean
}) {
  const days = window.day_keys.map((d) => d.charAt(0).toUpperCase() + d.slice(1)).join(', ')
  return (
    <div className={styles.windowRow}>
      <div className={styles.windowMeta}>
        <div className={styles.windowHead}>
          <span className={styles.windowLabel}>{window.label || days}</span>
          {active && <span className={styles.activeTag}>ACTIVE NOW</span>}
          {!window.enabled && <span className={styles.offTag}>DISABLED</span>}
        </div>
        <div className={styles.windowDetail}>
          {days} · {window.start}–{window.end}
          {window.wraps_midnight ? ' (past midnight)' : ''} · {window.timezone}
        </div>
        <div className={styles.chips}>
          {window.categories.map((key) => (
            <span key={key} className={styles.chip}>
              {labelFor(categories, key)}
            </span>
          ))}
        </div>
      </div>
      <button type="button" className={styles.removeButton} onClick={onRemove} disabled={removing}>
        REMOVE
      </button>
    </div>
  )
}

/**
 * Windows list + add form. The banner above is the primary legibility
 * surface ("is the stricter list on now, when does it revert") — this list
 * is the supporting detail an operator drops into only when they want to
 * change *what's scheduled*, not to find out what's active right now.
 *
 * A window only ever adds categories (ADR-0055) — there is no "remove"
 * category option here, and no enabled/disabled toggle on an existing
 * window because the API exposes only add + delete, not a patch.
 */
export default function WebFilterSchedule({ agentId, schedule, categories }: WebFilterScheduleProps) {
  const addWindow = useAddWebfilterWindow(agentId)
  const removeWindow = useRemoveWebfilterWindow(agentId)
  const [pendingRemove, setPendingRemove] = useState<string | null>(null)

  const [label, setLabel] = useState('')
  const [days, setDays] = useState<Set<string>>(new Set())
  const [start, setStart] = useState('21:00')
  const [end, setEnd] = useState('07:00')
  const [tz, setTz] = useState(schedule.timezone || browserTimezone())
  const [selected, setSelected] = useState<Set<string>>(new Set())

  const activeIds = new Set(schedule.active_windows.map((w) => w.id))

  function toggleSet(set: Set<string>, setter: (s: Set<string>) => void, key: string) {
    const next = new Set(set)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    setter(next)
  }

  function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    if (days.size === 0 || selected.size === 0) return
    addWindow.mutate(
      {
        days: Array.from(days),
        start,
        end,
        categories: Array.from(selected),
        label: label.trim(),
        timezone: tz.trim() || undefined,
      },
      {
        onSuccess: () => {
          setLabel('')
          setDays(new Set())
          setSelected(new Set())
        },
      },
    )
  }

  function onRemove(windowId: string) {
    setPendingRemove(windowId)
    removeWindow.mutate(windowId, { onSettled: () => setPendingRemove(null) })
  }

  return (
    <div>
      {schedule.windows.length === 0 ? (
        <p className={styles.empty}>No windows yet — add one below to add categories automatically at set times.</p>
      ) : (
        <div className={styles.windowList}>
          {schedule.windows.map((w) => (
            <WindowRow
              key={w.id}
              window={w}
              categories={categories}
              active={activeIds.has(w.id)}
              onRemove={() => onRemove(w.id)}
              removing={pendingRemove === w.id}
            />
          ))}
        </div>
      )}

      <form className={styles.form} onSubmit={onSubmit}>
        <div className={styles.formRow}>
          <input
            className={styles.input}
            placeholder="label (optional)"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
          />
          <input
            className={styles.input}
            placeholder="timezone, e.g. Europe/Berlin"
            value={tz}
            onChange={(e) => setTz(e.target.value)}
          />
        </div>
        <div className={styles.formRow}>
          <label className={styles.timeField}>
            start
            <input
              type="time"
              className={styles.input}
              value={start}
              onChange={(e) => setStart(e.target.value)}
              required
            />
          </label>
          <label className={styles.timeField}>
            end
            <input type="time" className={styles.input} value={end} onChange={(e) => setEnd(e.target.value)} required />
          </label>
        </div>

        <div className={styles.fieldLabel}>DAYS</div>
        <div className={styles.pickRow}>
          {DAY_OPTIONS.map((d) => (
            <label key={d.key} className={styles.pickChip}>
              <input type="checkbox" checked={days.has(d.key)} onChange={() => toggleSet(days, setDays, d.key)} />
              {d.label}
            </label>
          ))}
        </div>

        <div className={styles.fieldLabel}>ADDS THESE CATEGORIES</div>
        <div className={styles.pickRow}>
          {categories.map((c) => (
            <label key={c.key} className={styles.pickChip}>
              <input
                type="checkbox"
                checked={selected.has(c.key)}
                onChange={() => toggleSet(selected, setSelected, c.key)}
              />
              {c.label}
            </label>
          ))}
        </div>

        <div className={styles.formActions}>
          <button
            type="submit"
            className={styles.addButton}
            disabled={addWindow.isPending || days.size === 0 || selected.size === 0}
          >
            {addWindow.isPending ? 'ADDING…' : 'ADD WINDOW'}
          </button>
          {days.size === 0 || selected.size === 0 ? (
            <span className={styles.hint}>Pick at least one day and one category.</span>
          ) : null}
        </div>
        {addWindow.isError && (
          <p className={styles.error}>
            {addWindow.error instanceof Error ? addWindow.error.message : 'Could not add that window.'}
          </p>
        )}
      </form>
    </div>
  )
}

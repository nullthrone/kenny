import { Fragment, useState } from 'react'
import type { HardwareAerEntry, HardwareAppCrashes, HardwareEdacEntry, HardwareErrorGroup, HardwareErrorsSection } from '../types'
import { formatRelativeTime } from '../format'
import { finite, formatCount, isNonZero, sortedDays } from './hardwareFormat'
import { EmptyNote, Eyebrow, HwChip, Note, StatList, type Tone } from './HardwareParts'
import styles from './HardwareErrorsBody.module.css'

export interface HardwareErrorsBodyProps {
  hardware: HardwareErrorsSection
  /** The health rule's structured evidence (`HostSection.details`), when present. */
  details?: Record<string, unknown>
}

function levelTone(level: string): Tone | undefined {
  switch (level.toLowerCase()) {
    case 'critical':
    case 'error':
      return 'alert'
    case 'warning':
      return 'warn'
    default:
      return undefined
  }
}

export interface Finding {
  text: string
  component?: string
  tone?: Tone
}

/**
 * The server verdict's findings, when the rule attached them. The rule's one-line
 * `reason` is already in the modal header; `details.findings` is the itemised
 * list behind it. The server's `_rule_hardware_errors` emits objects carrying
 * `symptom`, `component` and `status` (kenny_server/health_rules.py); a list of
 * sentences or `reason`/`text`/`message` objects is tolerated too, and so is its
 * absence: a verdict without evidence just has no findings block.
 */
export function readFindings(details: Record<string, unknown> | undefined): Finding[] {
  const raw = details?.findings
  if (!Array.isArray(raw)) return []
  const out: Finding[] = []
  for (const item of raw) {
    if (typeof item === 'string' && item.trim()) {
      out.push({ text: item })
    } else if (item && typeof item === 'object') {
      const o = item as Record<string, unknown>
      const text = [o.symptom, o.reason, o.text, o.message].find((v): v is string => typeof v === 'string' && v.trim() !== '')
      if (!text) continue
      const status = typeof o.status === 'string' ? o.status : typeof o.severity === 'string' ? o.severity : ''
      out.push({
        text,
        component: typeof o.component === 'string' ? o.component : undefined,
        tone: status === 'crit' ? 'alert' : status === 'warn' ? 'warn' : undefined,
      })
    }
  }
  return out
}

/** Days with at least one event, from the group's `by_day`. */
function activeDays(group: HardwareErrorGroup): string[] {
  const byDay = group.by_day ?? {}
  return sortedDays(byDay).filter((d) => (byDay[d] ?? 0) > 0)
}

function groupKey(g: HardwareErrorGroup): string {
  return `${g.source}#${g.event_id}`
}

/** `details` as `[key, [[value, count], …]]`, most frequent value first, empty keys dropped. */
function detailEntries(group: HardwareErrorGroup): [string, [string, number][]][] {
  return Object.entries(group.details ?? {})
    .map(([key, values]): [string, [string, number][]] => [
      key,
      Object.entries(values ?? {}).sort(([, a], [, b]) => b - a),
    ])
    .filter(([, values]) => values.length > 0)
}

function GroupTable({ groups }: { groups: HardwareErrorGroup[] }) {
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set())

  function toggle(key: string) {
    setOpen((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">Source</th>
            <th scope="col" className={styles.num}>Count</th>
            <th scope="col">Last seen</th>
            <th scope="col">Active</th>
            <th scope="col">Sample</th>
          </tr>
        </thead>
        <tbody>
          {groups.map((g) => {
            const key = groupKey(g)
            const days = activeDays(g)
            const entries = detailEntries(g)
            const expanded = open.has(key)
            const tone = levelTone(g.level)
            return (
              <Fragment key={key}>
                <tr>
                  <td>
                    <div className={styles.source}>{g.source}</div>
                    <div className={styles.meta}>
                      {g.event_id !== 0 && <span>#{g.event_id}</span>}
                      <span className={styles.level} data-tone={tone}>{g.level.toUpperCase()}</span>
                    </div>
                    {entries.length > 0 && (
                      <button
                        type="button"
                        className={styles.toggle}
                        aria-expanded={expanded}
                        onClick={() => toggle(key)}
                      >
                        {expanded ? 'HIDE DETAILS' : 'DETAILS'}
                      </button>
                    )}
                  </td>
                  <td className={styles.num}>{formatCount(g.count)}×</td>
                  <td>{formatRelativeTime(g.last_seen)}</td>
                  <td title={days.join(', ')}>
                    {days.length} day{days.length === 1 ? '' : 's'}
                  </td>
                  <td className={styles.sample}>{g.sample || '—'}</td>
                </tr>
                {expanded && entries.length > 0 && (
                  <tr className={styles.detailRow}>
                    <td colSpan={5}>
                      <dl className={styles.details}>
                        {entries.map(([name, values]) => (
                          <div key={name} className={styles.detail}>
                            <dt>{name}</dt>
                            <dd>
                              {values.map(([value, count]) => (
                                <span key={value} className={styles.detailValue}>
                                  {value} <span className={styles.detailCount}>×{count}</span>
                                </span>
                              ))}
                            </dd>
                          </div>
                        ))}
                      </dl>
                      <p className={styles.detailNote}>
                        Counts cover only the newest events of this group whose details were read.
                      </p>
                    </td>
                  </tr>
                )}
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function AppCrashes({ crashes }: { crashes: HardwareAppCrashes }) {
  const codes = Object.entries(crashes.exception_codes ?? {}).sort(([, a], [, b]) => b - a)
  const days = sortedDays(crashes.by_day).filter((d) => (crashes.by_day?.[d] ?? 0) > 0)
  return (
    <>
      <Eyebrow>APPLICATION CRASHES</Eyebrow>
      <StatList
        items={[
          { label: 'Crashes', value: formatCount(crashes.total) },
          { label: 'Distinct applications', value: formatCount(crashes.distinct_apps) },
          { label: 'Distinct modules', value: formatCount(crashes.distinct_modules) },
          { label: 'Days with crashes', value: days.length ? String(days.length) : '—' },
        ]}
      />
      {codes.length > 0 && (
        <p className={styles.codes}>
          {codes.map(([code, n]) => (
            <span key={code} className={styles.detailValue}>
              {code} <span className={styles.detailCount}>×{n}</span>
            </span>
          ))}
        </p>
      )}
    </>
  )
}

function counterCell(value: number | null | undefined, tone: Tone) {
  return (
    <td className={styles.num} data-tone={isNonZero(value) ? tone : undefined}>
      {formatCount(value)}
    </td>
  )
}

function EdacTable({ entries }: { entries: HardwareEdacEntry[] }) {
  return (
    <>
      <Eyebrow>MEMORY CONTROLLERS (EDAC)</Eyebrow>
      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th scope="col">Controller</th>
              <th scope="col" className={styles.num}>Corrected</th>
              <th scope="col" className={styles.num}>Uncorrected</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e) => (
              <tr key={e.controller}>
                <td className={styles.source}>{e.controller}</td>
                {counterCell(e.ce_count, 'warn')}
                {counterCell(e.ue_count, 'alert')}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

function AerTable({ entries }: { entries: HardwareAerEntry[] }) {
  return (
    <>
      <Eyebrow>PCIE ERRORS (AER)</Eyebrow>
      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th scope="col">Device</th>
              <th scope="col" className={styles.num}>Correctable</th>
              <th scope="col" className={styles.num}>Non-fatal</th>
              <th scope="col" className={styles.num}>Fatal</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e) => (
              <tr key={e.device}>
                <td className={styles.source}>{e.device}</td>
                {counterCell(e.correctable, 'warn')}
                {counterCell(e.nonfatal, 'alert')}
                {counterCell(e.fatal, 'alert')}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

/**
 * Hardware-relevant event groups (`snapshot.hardware_errors`, docs/protocol.md):
 * corrected/uncorrected machine-check and PCIe errors, graphics-driver resets,
 * storage retries and unexpected power loss, plus application-crash diversity and
 * the Linux EDAC / AER counters. The agent reports facts only; which component
 * is suspect and whether it is a finding is the server's verdict, shown first.
 *
 * Deliberately independent of `ReliabilityBody`: no suppression rules and no
 * classifier annotations — these groups carry none.
 */
export default function HardwareErrorsBody({ hardware, details }: HardwareErrorsBodyProps) {
  const findings = readFindings(details)
  const groups = hardware.groups ?? []
  const windowDays = finite(hardware.window_days)
  const effective = finite(hardware.effective_window_days)
  // A System log that wrapped after three days must not read as eleven quiet ones.
  const shortWindow = windowDays !== null && effective !== null && effective < windowDays
  const dropped = hardware.truncated_count ?? 0
  const crashes = hardware.app_crashes
  const edac = hardware.edac ?? []
  const aer = hardware.aer ?? []
  const errors = hardware.errors ?? []

  return (
    <div>
      {hardware.summary && <p className={styles.summary}>{hardware.summary}</p>}
      {shortWindow && (
        <Note tone="warn">
          Event log only reaches back {effective} day{effective === 1 ? '' : 's'} (queried {windowDays}): a quiet
          window here is shorter than it looks.
        </Note>
      )}

      {findings.length > 0 && (
        <>
          <Eyebrow>FINDINGS</Eyebrow>
          <ul className={styles.findings}>
            {findings.map((f, i) => (
              <li key={i} className={styles.finding} data-tone={f.tone}>
                {f.component && <HwChip tone={f.tone}>{f.component.toUpperCase()}</HwChip>}
                <span>{f.text}</span>
              </li>
            ))}
          </ul>
        </>
      )}

      <Eyebrow>
        EVENT GROUPS · {groups.length}
        {hardware.truncated ? '+' : ''}
      </Eyebrow>
      {groups.length === 0 ? (
        <EmptyNote>No hardware-relevant events in the window.</EmptyNote>
      ) : (
        <GroupTable groups={groups} />
      )}
      {hardware.truncated && dropped > 0 && (
        <Note>{dropped} more group{dropped === 1 ? '' : 's'} not reported (the agent keeps the most recent and the largest).</Note>
      )}

      {crashes && <AppCrashes crashes={crashes} />}
      {edac.length > 0 && <EdacTable entries={edac} />}
      {aer.length > 0 && <AerTable entries={aer} />}

      {errors.length > 0 && (
        <>
          <Eyebrow>PROBE ERRORS</Eyebrow>
          <ul className={styles.errors}>
            {errors.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}

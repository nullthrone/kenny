import { useState } from 'react'
import type { Severity } from '../../api/types'
import SeverityChip from '../../components/SeverityChip/SeverityChip'
import { formatAge } from '../inbox/age'
import styles from './LinkedAlerts.module.css'

/** One `kind='alert'` event belonging to this ticket. */
export interface LinkedAlert {
  id: number
  at: string
  agent_id: string | null
  level: string
  title: string
  body: string
  priority: string
  event_type: string
}

/** The live verdict of one section this ticket is about. */
export interface LinkedFinding {
  name: string
  status: string
  summary: string
  reason: string
  since: string
  age_seconds: number
}

export interface TicketAlertsResponse {
  ticket_id: string
  agent_id: string
  collected_at: string
  alerts: LinkedAlert[]
  findings: LinkedFinding[]
}

const SEVERITIES = new Set(['ok', 'posture', 'warn', 'crit', 'unknown'])

/** The server's status vocabulary is closed; anything else reads as unknown. */
function asSeverity(status: string): Severity {
  return (SEVERITIES.has(status) ? status : 'unknown') as Severity
}

function formatTime(iso: string): string {
  const ts = new Date(iso)
  return Number.isNaN(ts.getTime()) ? iso : ts.toLocaleString()
}

/**
 * What fired on this ticket, and whether what it reported still holds.
 *
 * Everything opens in place. This panel renders no link and no navigation:
 * reading an alert is part of working the ticket, and a queue that answers a
 * click by replacing the screen is the behaviour this surface exists to
 * remove. `views/inbox/LinkedAlerts.test.tsx` pins that.
 *
 * Renders nothing when the ticket has neither — every human-opened ticket, and
 * an alert ticket old enough that its events have aged out of retention (~30
 * days), which is a normal state and not an error.
 */
export default function LinkedAlerts({ data }: { data: TicketAlertsResponse }) {
  const [open, setOpen] = useState<Set<string>>(new Set())

  function toggle(key: string) {
    setOpen((prev) => {
      const next = new Set(prev)
      if (!next.delete(key)) next.add(key)
      return next
    })
  }

  if (data.alerts.length === 0 && data.findings.length === 0) return null

  return (
    <div className={styles.root} data-shot="linked-alerts">
      {data.findings.length > 0 && (
        <section className={styles.block}>
          <h2 className={`kc-caps ${styles.heading}`}>Current state</h2>
          <ul className={styles.list}>
            {data.findings.map((f) => {
              const key = `finding:${f.name}`
              const isOpen = open.has(key)
              return (
                <li key={key} className={styles.item}>
                  <button
                    type="button"
                    className={styles.entry}
                    aria-expanded={isOpen}
                    onClick={() => toggle(key)}
                  >
                    <SeverityChip severity={asSeverity(f.status)} className={styles.chip} />
                    <span className={styles.name}>{f.name.replace(/_/g, ' ')}</span>
                    <span className={styles.detail}>{f.reason || f.summary}</span>
                    {f.age_seconds > 0 && <span className={styles.age}>{formatAge(f.age_seconds)}</span>}
                  </button>
                  {isOpen && (
                    <div className={styles.expanded}>
                      {f.summary && <p className={styles.body}>{f.summary}</p>}
                      <p className={styles.faint}>
                        {f.since ? `${f.status} since ${formatTime(f.since)}` : `currently ${f.status}`}
                        {data.collected_at ? ` · last seen ${formatTime(data.collected_at)}` : ''}
                      </p>
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        </section>
      )}

      {data.alerts.length > 0 && (
        <section className={styles.block}>
          <h2 className={`kc-caps ${styles.heading}`}>Alert history</h2>
          <ul className={styles.list}>
            {data.alerts.map((a) => {
              const key = `alert:${a.id}`
              const isOpen = open.has(key)
              return (
                <li key={key} className={styles.item}>
                  <button
                    type="button"
                    className={styles.entry}
                    aria-expanded={isOpen}
                    onClick={() => toggle(key)}
                  >
                    <SeverityChip
                      severity={a.level === 'crit' ? 'crit' : a.level === 'warn' ? 'warn' : 'ok'}
                      className={styles.chip}
                    />
                    <span className={styles.name}>{formatTime(a.at)}</span>
                    <span className={styles.detail}>{a.title}</span>
                  </button>
                  {isOpen && (
                    <div className={styles.expanded}>
                      {a.body && <p className={styles.body}>{a.body}</p>}
                      <p className={styles.faint}>
                        {[a.event_type, a.priority && `priority ${a.priority}`, a.agent_id]
                          .filter(Boolean)
                          .join(' · ')}
                      </p>
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        </section>
      )}
    </div>
  )
}

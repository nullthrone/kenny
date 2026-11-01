import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import styles from './Transcript.module.css'

/** One entry of `GET /api/stewards/commands?host_id=` (`stewards/commands.py::command_actions`). */
export interface CommandAction {
  steward_id: string
  title: string
  summary: string
  mode: string
  preview_allowed: boolean
  run_allowed: boolean
}

export interface StewardQuickActionsProps {
  hostId: string
  /** Puts the steward's run card into the conversation. Starts nothing. */
  onPick: (stewardId: string, hostId: string, reason: string) => void
}

/**
 * The stewards an operator could run on this PC, offered in the empty
 * conversation of a host page. Choosing one adds the same card kenny's
 * proposal makes -- with its PREVIEW and RUN NOW buttons -- and nothing more:
 * no run exists until a button on that card is pressed.
 *
 * Nothing is shown while the list loads, when it cannot be read, or when no
 * steward qualifies: an empty conversation has no use for a message about that.
 */
export default function StewardQuickActions({ hostId, onPick }: StewardQuickActionsProps) {
  const [actions, setActions] = useState<CommandAction[]>([])

  useEffect(() => {
    let live = true
    api
      .get<{ stewards: CommandAction[] }>(`/api/stewards/commands?host_id=${encodeURIComponent(hostId)}`)
      .then((res) => {
        if (live) setActions(Array.isArray(res?.stewards) ? res.stewards : [])
      })
      .catch(() => {
        if (live) setActions([])
      })
    return () => {
      live = false
    }
  }, [hostId])

  if (actions.length === 0) return null

  return (
    <div className={styles.quickActions} data-testid="steward-quick-actions">
      <span className={styles.quickActionsLabel}>RUN A STEWARD ON {hostId.toUpperCase()}</span>
      {actions.map((a) => (
        <button
          key={a.steward_id}
          type="button"
          className={styles.quickAction}
          onClick={() => onPick(a.steward_id, hostId, 'You chose this from the quick actions.')}
        >
          {a.title}
          {a.summary && <span className={styles.quickActionSummary}>{a.summary}</span>}
        </button>
      ))}
    </div>
  )
}

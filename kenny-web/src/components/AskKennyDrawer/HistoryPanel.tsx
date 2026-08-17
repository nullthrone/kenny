import { useEffect, useState, type KeyboardEvent, type MouseEvent } from 'react'
import type { ConversationSummary } from '../../chat/types'
import { Trash2, ICON_STROKE_WIDTH } from '../icons'
import styles from './HistoryPanel.module.css'

export interface HistoryPanelProps {
  listHistory: () => Promise<ConversationSummary[]>
  onSelect: (id: string) => void
  deleteConversation: (id: string) => Promise<void>
}

export default function HistoryPanel({ listHistory, onSelect, deleteConversation }: HistoryPanelProps) {
  const [state, setState] = useState<{ loading: boolean; error: string | null; rows: ConversationSummary[] }>({
    loading: true,
    error: null,
    rows: [],
  })

  useEffect(() => {
    let cancelled = false
    listHistory()
      .then((rows) => {
        if (!cancelled) setState({ loading: false, error: null, rows })
      })
      .catch((err: unknown) => {
        if (!cancelled) setState({ loading: false, error: err instanceof Error ? err.message : String(err), rows: [] })
      })
    return () => {
      cancelled = true
    }
  }, [listHistory])

  async function handleDelete(id: string, e: { stopPropagation: () => void }) {
    e.stopPropagation()
    await deleteConversation(id)
    setState((s) => ({ ...s, rows: s.rows.filter((r) => r.id !== id) }))
  }

  if (state.loading) return <div className={styles.root}><p className={styles.status}>Loading conversations…</p></div>
  if (state.error) return <div className={styles.root}><p className={styles.status}>Could not load history: {state.error}</p></div>
  if (state.rows.length === 0) return <div className={styles.root}><p className={styles.status}>No past conversations yet.</p></div>

  return (
    <div className={styles.root}>
      {state.rows.map((row) => (
        <button
          key={row.id}
          type="button"
          className={styles.row}
          data-shot={`history-row-${row.id}`}
          onClick={() => onSelect(row.id)}
        >
          <div className={styles.rowMain}>
            <div className={styles.rowTitle}>{row.title || 'Untitled conversation'}</div>
            <div className={styles.rowMeta}>
              {row.agent_id || 'fleet'} · {row.updated_at}
            </div>
          </div>
          <span
            className={styles.delete}
            role="button"
            tabIndex={0}
            aria-label="Delete conversation"
            onClick={(e: MouseEvent) => void handleDelete(row.id, e)}
            onKeyDown={(e: KeyboardEvent) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                void handleDelete(row.id, e)
              }
            }}
          >
            <Trash2 width={14} height={14} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          </span>
        </button>
      ))}
    </div>
  )
}

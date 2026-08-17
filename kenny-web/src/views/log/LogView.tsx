import { useEffect, useMemo, useState } from 'react'
import { useInfiniteQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import type { LogKind, LogResponse, LogRow } from '../../api/types'
import Chip from '../../components/Chip/Chip'
import EmptyState from '../../components/EmptyState/EmptyState'
import { ScrollText } from '../../components/icons'
import styles from './LogView.module.css'

const FILTERS: { label: string; kind: LogKind | null }[] = [
  { label: 'ALL', kind: null },
  { label: 'TOOLS', kind: 'tools' },
  { label: 'ALERTS', kind: 'alerts' },
  { label: 'EVENTS', kind: 'events' },
]

/**
 * Tag colour is a presentation heuristic over `LogRow.tag` — the API carries
 * no colour/severity for a row, only the short caps string (`TOOL`, `ALERT`,
 * `WARN`, `PUSH`, `INFO`, …). Unknown tags fall back to muted rather than
 * guessing at a severity that was never sent.
 */
function tagColor(tag: string): string {
  switch (tag.toUpperCase()) {
    case 'TOOL':
      return 'var(--brass-600)'
    case 'ALERT':
      return 'var(--danger)'
    case 'WARN':
      return 'var(--warn)'
    default:
      return 'var(--text-muted)'
  }
}

function formatTime(ts: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false })
}

/** Commits `value` after `delayMs` of no further changes — keeps the text filter from firing a request per keystroke. */
function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delayMs)
    return () => clearTimeout(t)
  }, [value, delayMs])
  return debounced
}

/**
 * `#/log` — the merged tool/alert/event stream. Filtering and paging are
 * SERVER-SIDE (`GET /api/log?kind=&q=&cursor=`): the view never fetches
 * everything and filters client-side, unlike the old Activity tab.
 */
export default function LogView() {
  const [kind, setKind] = useState<LogKind | null>(null)
  const [queryText, setQueryText] = useState('')
  const q = useDebounced(queryText, 300)

  const { data, isLoading, isError, error, fetchNextPage, hasNextPage, isFetchingNextPage } = useInfiniteQuery({
    queryKey: ['log', kind, q],
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams()
      if (kind) params.set('kind', kind)
      if (q) params.set('q', q)
      if (pageParam) params.set('cursor', pageParam)
      const qs = params.toString()
      return api.get<LogResponse>(`/api/log${qs ? `?${qs}` : ''}`)
    },
    initialPageParam: '' as string,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  })

  const rows: LogRow[] = useMemo(() => data?.pages.flatMap((page) => page.rows) ?? [], [data])

  return (
    <div className={`kc-content kc-view ${styles.root}`}>
      <h1 className="kc-h1" style={{ fontFamily: 'var(--font-display)', fontWeight: 500, fontSize: 'var(--display-md)', margin: '0 0 6px' }}>
        Log
      </h1>
      <p className={styles.lead}>Every tool call, alert, and log line across the fleet — one stream, filtered.</p>

      <div className={styles.filters}>
        {FILTERS.map((f) => (
          <Chip key={f.label} label={f.label} active={kind === f.kind} onClick={() => setKind(f.kind)} />
        ))}
        <input
          className={styles.search}
          placeholder="Filter by tool, host, or text…"
          value={queryText}
          onChange={(e) => setQueryText(e.target.value)}
          aria-label="Filter the log by tool, host, or text"
        />
      </div>

      {isLoading && <div className={styles.loading}>Loading…</div>}

      {isError && (
        <EmptyState
          title="Could not load the log"
          message={error instanceof Error ? error.message : 'Something went wrong. Try reloading.'}
        />
      )}

      {!isLoading && !isError && rows.length === 0 && (
        <EmptyState icon={ScrollText} title="Nothing here" message="No log rows match this filter." />
      )}

      {rows.length > 0 && (
        <div className={styles.list}>
          {rows.map((r, i) => (
            <div key={`${r.ts}-${r.kind}-${i}`} className={styles.row}>
              <span className={styles.time}>{formatTime(r.ts)}</span>
              <span className={styles.tag} style={{ color: tagColor(r.tag) }}>
                {r.tag}
              </span>
              <span className={`${styles.line} kc-logline`}>
                <span className={styles.what}>{r.what}</span> <span className={styles.msg}>{r.message}</span>
              </span>
              <span className={styles.host}>{r.host ?? '—'}</span>
            </div>
          ))}
        </div>
      )}

      {hasNextPage && (
        <div className={styles.olderWrap}>
          <button type="button" className={styles.olderBtn} onClick={() => fetchNextPage()} disabled={isFetchingNextPage}>
            {isFetchingNextPage ? 'LOADING…' : 'OLDER →'}
          </button>
        </div>
      )}
    </div>
  )
}

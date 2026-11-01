import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import type { StewardRun } from '../../views/admin/types'

/** How often a started run is read again. */
export const RUN_POLL_MS = 3000
/** A run still going after this long is no longer watched from the card (the Stewards view still shows it). */
export const RUN_POLL_MAX_MS = 15 * 60 * 1000

export function isFinished(status: string | undefined): boolean {
  return status === 'completed' || status === 'failed' || status === 'skipped'
}

/** The part of a run the card shows: what it is, how it went, and how big it was. Numbers and enumerations only. */
export interface RunSummary {
  status: string
  verdict: string | null
  actions: number
  recommendations: number
  /** Sum of `result.bytes_freed` over the actions that recorded one; null when none did. */
  bytesFreed: number | null
}

export function summarizeRun(run: StewardRun): RunSummary {
  let freed: number | null = null
  for (const action of run.actions ?? []) {
    const n = action.result?.bytes_freed
    if (typeof n === 'number' && Number.isFinite(n) && n >= 0) freed = (freed ?? 0) + n
  }
  return {
    status: run.status,
    verdict: run.verdict ?? null,
    actions: run.actions?.length ?? 0,
    recommendations: run.recommendations?.length ?? 0,
    bytesFreed: freed,
  }
}

export interface RunWatch {
  run: StewardRun | null
  /** The last read failed (the run is read again, unless it is gone). */
  error: string | null
  /** The run was still going when watching stopped. */
  gaveUp: boolean
}

/**
 * Watch one run: read `GET /api/stewards/runs/{id}` now and every {@link RUN_POLL_MS},
 * until the run has finished or {@link RUN_POLL_MAX_MS} have passed.
 *
 * Plain fetch and timers, not a query: the drawer is rendered without a query
 * client in places (see `StewardRunProposalCard`), and a card that cannot be
 * rendered there cannot be tested there either.
 */
export function useStewardRunStatus(runId: string | null): RunWatch {
  const [watch, setWatch] = useState<RunWatch>({ run: null, error: null, gaveUp: false })

  useEffect(() => {
    if (!runId) return undefined
    let live = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const startedAt = Date.now()
    setWatch({ run: null, error: null, gaveUp: false })

    const tick = async () => {
      let gone = false
      try {
        const run = await api.get<StewardRun>(`/api/stewards/runs/${encodeURIComponent(runId)}`)
        if (!live) return
        setWatch((w) => ({ ...w, run, error: null }))
        if (isFinished(run.status)) return
      } catch (err) {
        if (!live) return
        gone = (err as { status?: number }).status === 404
        setWatch((w) => ({ ...w, error: (err as Error).message || 'Could not read the run.' }))
      }
      if (gone) return
      if (Date.now() - startedAt >= RUN_POLL_MAX_MS) {
        setWatch((w) => ({ ...w, gaveUp: true }))
        return
      }
      timer = setTimeout(() => void tick(), RUN_POLL_MS)
    }
    void tick()

    return () => {
      live = false
      if (timer !== undefined) clearTimeout(timer)
    }
  }, [runId])

  return watch
}

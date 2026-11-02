import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { OutdatedAgent, UpdateOutdatedResult } from './types'

export const OUTDATED_AGENTS_KEY = ['outdated-agents'] as const

/**
 * `GET /api/agents/outdated` — the agents whose reported version is older than the
 * binary staged for them. A plain version comparison on the server; nothing here
 * changes a machine.
 *
 * Refetched on an interval because the answer moves without anyone clicking: a PC
 * reconnects, or a release is staged. `retry` is off so a server that predates the
 * route simply shows no banner instead of retrying a 404.
 */
export function useOutdatedAgents(enabled: boolean = true) {
  return useQuery({
    queryKey: OUTDATED_AGENTS_KEY,
    queryFn: () => api.get<{ agents: OutdatedAgent[] }>('/api/agents/outdated'),
    enabled,
    retry: false,
    refetchInterval: 60_000,
  })
}

/**
 * `POST /api/agents/update-outdated` — send the staged binary to the online agents
 * that are behind it, optionally only those named in `agents`. One agent failing
 * does not stop the others; the response lists each outcome. Operator+ server-side.
 */
export function useUpdateOutdated() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (agents?: string[]) =>
      api.post<{ ok: boolean; sent: number; results: UpdateOutdatedResult[] }>(
        '/api/agents/update-outdated',
        agents ? { agents } : undefined,
      ),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: OUTDATED_AGENTS_KEY })
      void queryClient.invalidateQueries({ queryKey: ['fleet'] })
    },
  })
}

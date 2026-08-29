import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { AgentBinaryStatus } from './types'

export const AGENT_BINARY_KEY = ['agent-binary'] as const

/**
 * `GET /api/agent-binary` — what the server has staged to hand a new PC.
 *
 * Shared by every surface that needs it, so they cost one request between them:
 * the About dialog (staged version), Fleet's banner, and the Add-a-PC wizard's
 * target list. It lives here rather than beside one of those callers because no
 * single one of them owns it.
 *
 * Best-effort throughout, like the legacy dashboard's `.catch(() => null)`: the
 * endpoint reports availability, so being unable to read it must degrade to
 * "we don't know" rather than blocking provisioning outright. `retry` is off for
 * the same reason — the handler does no network I/O of its own, so a failure is
 * a real one worth showing rather than a blip to paper over.
 */
export function useAgentBinary(enabled: boolean = true) {
  return useQuery({
    queryKey: AGENT_BINARY_KEY,
    queryFn: () => api.get<AgentBinaryStatus>('/api/agent-binary'),
    enabled,
    retry: false,
  })
}

/**
 * `POST /api/agent-binary/fetch` — go ask GitHub for the agent release now,
 * instead of waiting for the next restart.
 *
 * The server fetches the binary once at startup when a GitHub token is configured
 * (ADR-0015). When that attempt failed — the token was wrong, GitHub was down,
 * no release matched — the fleet is left with no installer to hand out and, until
 * this route is called, the only remedy is restarting the container. Operator+
 * server-side.
 *
 * Invalidates the status on settle, success or not: a failed retry still updates
 * `last_fetch`, which is the message the banner shows.
 */
export function useRetryAgentBinaryFetch() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.post<AgentBinaryStatus>('/api/agent-binary/fetch'),
    onSettled: () => queryClient.invalidateQueries({ queryKey: AGENT_BINARY_KEY }),
  })
}

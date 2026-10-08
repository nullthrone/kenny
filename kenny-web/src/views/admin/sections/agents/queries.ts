import { useQuery } from '@tanstack/react-query'
import { api, ApiError } from '../../../../api/client'
import type { FleetResponse } from '../../../../api/types'
import type {
  AgentAuthorizationsResponse,
  AgentParamsResponse,
  AgentRun,
  SpecializedAgentsResponse,
} from '../../types'

/**
 * Every query of this view lives under one prefix so a mutation can refresh the lot
 * with a single `invalidateQueries`: a parameter edit moves the effective hash,
 * the mode, the authorizations' state and (through the hash) what a new run is bound to.
 */
export const AGENTS_KEY = ['admin', 'agents'] as const

const base = (agentId: string) => `/api/specialized-agents/${encodeURIComponent(agentId)}`

export function useAgents() {
  return useQuery({
    queryKey: [...AGENTS_KEY, 'list'] as const,
    queryFn: () => api.get<SpecializedAgentsResponse>('/api/specialized-agents'),
  })
}

export function useAgentParams(agentId: string) {
  return useQuery({
    queryKey: [...AGENTS_KEY, 'params', agentId] as const,
    queryFn: () => api.get<AgentParamsResponse>(`${base(agentId)}/params`),
  })
}

export function useAgentAuthorizations(agentId: string) {
  return useQuery({
    queryKey: [...AGENTS_KEY, 'authorizations', agentId] as const,
    queryFn: () => api.get<AgentAuthorizationsResponse>(`${base(agentId)}/authorizations`),
  })
}

export function useAgentRuns(agentId: string, limit = 25) {
  return useQuery({
    queryKey: [...AGENTS_KEY, 'runs', agentId, limit] as const,
    queryFn: () =>
      api
        .get<{ runs: AgentRun[] }>(
          `/api/specialized-agents/runs?agent_id=${encodeURIComponent(agentId)}&limit=${limit}`,
        )
        .then((r) => r.runs),
  })
}

export function useAgentRun(runId: string | null) {
  return useQuery({
    queryKey: [...AGENTS_KEY, 'run', runId] as const,
    queryFn: () => api.get<AgentRun>(`/api/specialized-agents/runs/${encodeURIComponent(runId ?? '')}`),
    enabled: runId !== null,
  })
}

/** The fleet, for host pickers. Shared key with every other admin picker. */
export function useFleetHosts() {
  return useQuery({ queryKey: ['fleet'], queryFn: () => api.get<FleetResponse>('/api/fleet') })
}

/**
 * What a refused consent write (mode, parameters, grant, revoke) means to the person
 * who made it. The server's reason is the fallback; the two statuses that have a
 * fixed meaning (ADR-0072 rule 2) get a sentence that says what to do.
 */
export function consentErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    if (err.status === 409) {
      return 'The agent changed since you loaded it — reload and review it, then try again.'
    }
    if (err.status === 403) {
      return 'Sign in to the dashboard as a superuser to do this. A personal access token, an OAuth token or the shared operator token cannot.'
    }
    if (err.message) return err.message
  }
  return fallback
}

export function isHashChanged(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409
}

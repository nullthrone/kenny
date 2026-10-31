import { useQuery } from '@tanstack/react-query'
import { api, ApiError } from '../../../../api/client'
import type { FleetResponse } from '../../../../api/types'
import type {
  StewardAuthorizationsResponse,
  StewardParamsResponse,
  StewardRun,
  StewardsResponse,
} from '../../types'

/**
 * Every query of this view lives under one prefix so a mutation can refresh the lot
 * with a single `invalidateQueries`: a parameter edit moves the effective hash,
 * the mode, the authorizations' state and (through the hash) what a new run is bound to.
 */
export const STEWARDS_KEY = ['admin', 'stewards'] as const

const base = (stewardId: string) => `/api/stewards/${encodeURIComponent(stewardId)}`

export function useStewards() {
  return useQuery({
    queryKey: [...STEWARDS_KEY, 'list'] as const,
    queryFn: () => api.get<StewardsResponse>('/api/stewards'),
  })
}

export function useStewardParams(stewardId: string) {
  return useQuery({
    queryKey: [...STEWARDS_KEY, 'params', stewardId] as const,
    queryFn: () => api.get<StewardParamsResponse>(`${base(stewardId)}/params`),
  })
}

export function useStewardAuthorizations(stewardId: string) {
  return useQuery({
    queryKey: [...STEWARDS_KEY, 'authorizations', stewardId] as const,
    queryFn: () => api.get<StewardAuthorizationsResponse>(`${base(stewardId)}/authorizations`),
  })
}

export function useStewardRuns(stewardId: string, limit = 25) {
  return useQuery({
    queryKey: [...STEWARDS_KEY, 'runs', stewardId, limit] as const,
    queryFn: () =>
      api
        .get<{ runs: StewardRun[] }>(
          `/api/stewards/runs?steward_id=${encodeURIComponent(stewardId)}&limit=${limit}`,
        )
        .then((r) => r.runs),
  })
}

export function useStewardRun(runId: string | null) {
  return useQuery({
    queryKey: [...STEWARDS_KEY, 'run', runId] as const,
    queryFn: () => api.get<StewardRun>(`/api/stewards/runs/${encodeURIComponent(runId ?? '')}`),
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
      return 'The steward changed since you loaded it — reload and review it, then try again.'
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

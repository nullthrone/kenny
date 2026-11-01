import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../../../api/client'
import type { FleetResponse } from '../../../../api/types'
import { FALLBACK_MAX_EXPIRY_DAYS, FALLBACK_NEVER_AUTHORIZED } from './badges'
import type {
  BuilderCatalog,
  BuilderClonePreview,
  BuilderData,
  BuilderDeleted,
  BuilderFieldError,
  BuilderSaved,
  BuiltStewardRecord,
  StewardAuthorizationsResponse,
  StewardParamsResponse,
  StewardRun,
  StewardTriggerConfig,
  StewardTriggersResponse,
  StewardTriggersSaved,
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

/** What may start one steward: its ceiling, its baseline, what is in force, and the hash a write must name. */
export function useStewardTriggers(stewardId: string) {
  return useQuery({
    queryKey: [...STEWARDS_KEY, 'triggers', stewardId] as const,
    queryFn: () => api.get<StewardTriggersResponse>(`${base(stewardId)}/triggers`),
  })
}

/**
 * Replace one steward's triggers, naming the hash the person was shown (a 409 when it moved).
 * A change moves the effective hash, drops the steward to shadow and voids its authorizations,
 * so on success every query of the view is refreshed (it shares the `STEWARDS_KEY` prefix
 * with the overview, the detail and the authorizations).
 */
export function useSaveStewardTriggers(stewardId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: { triggers: StewardTriggerConfig; effective_hash: string | null }) =>
      api.put<StewardTriggersSaved>(`${base(stewardId)}/triggers`, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: STEWARDS_KEY }),
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

/**
 * What the server lets a built steward be made of, and the two limits the authorization
 * rules share with it. Static for a given release, so it is read once and kept.
 */
export function useBuilderCatalog() {
  return useQuery({
    queryKey: [...STEWARDS_KEY, 'builder', 'catalog'] as const,
    queryFn: () => api.get<BuilderCatalog>('/api/stewards/builder/catalog'),
    staleTime: 5 * 60_000,
  })
}

/** The tools no authorization may ever cover, and the longest an authorization may last. */
export interface AuthorizationLimits {
  neverAuthorized: readonly string[]
  maxExpiryDays: number
  /**
   * `server` when the builder catalog supplied the values; `fallback` when the request failed
   * and the copies in `badges.tsx` stand in (the server enforces its own either way); `loading`
   * until the request settles, when the same copies are used without comment.
   */
  source: 'server' | 'fallback' | 'loading'
}

export function useAuthorizationLimits(): AuthorizationLimits {
  const catalog = useBuilderCatalog()
  if (catalog.data) {
    return {
      neverAuthorized: catalog.data.never_authorized,
      maxExpiryDays: catalog.data.max_expiry_days,
      source: 'server',
    }
  }
  return {
    neverAuthorized: FALLBACK_NEVER_AUTHORIZED,
    maxExpiryDays: FALLBACK_MAX_EXPIRY_DAYS,
    source: catalog.isError ? 'fallback' : 'loading',
  }
}

/** One built steward as the editor loads it (an invalid one included). */
export function useBuiltSteward(stewardId: string, enabled = true) {
  return useQuery({
    queryKey: [...STEWARDS_KEY, 'built', stewardId] as const,
    queryFn: () => api.get<BuiltStewardRecord>(`/api/stewards/custom/${encodeURIComponent(stewardId)}`),
    enabled,
  })
}

/** A shipped steward as built-steward data under `newId`, saving nothing: its data and what a built one may not keep. */
export function useCloneDraft(shippedId: string, newId: string) {
  return useQuery({
    queryKey: [...STEWARDS_KEY, 'builder', 'clone', shippedId, newId] as const,
    queryFn: () =>
      api.get<BuilderClonePreview>(
        `/api/stewards/builder/clone/${encodeURIComponent(shippedId)}?id=${encodeURIComponent(newId)}`,
      ),
    // A preview is a read of the code catalog at this moment, not something to keep around.
    gcTime: 0,
  })
}

export type SaveBuiltTarget =
  | { mode: 'create'; source: string }
  | { mode: 'edit'; stewardId: string; effectiveHash: string | null }

/**
 * Create or save a built steward. Either moves the steward to `shadow` and voids its
 * standing authorizations (a save is always a new spec hash), so every query of the view is
 * refreshed — and the mutation settles only after that refresh, so a caller that navigates
 * on success lands on a list that already has the steward.
 */
export function useSaveBuilt(target: SaveBuiltTarget) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (spec: BuilderData) =>
      target.mode === 'create'
        ? api.post<BuilderSaved>('/api/stewards/custom', { spec, source: target.source })
        : api.put<BuilderSaved>(`/api/stewards/custom/${encodeURIComponent(target.stewardId)}`, {
            spec,
            effective_hash: target.effectiveHash,
          }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: STEWARDS_KEY }),
  })
}

export function useDeleteBuilt(stewardId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.delete<BuilderDeleted>(`/api/stewards/custom/${encodeURIComponent(stewardId)}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: STEWARDS_KEY }),
  })
}

/** The field errors of a 400 from the builder endpoints (`{error, errors, data?}`), else `null`. */
export function builderFieldErrors(err: unknown): BuilderFieldError[] | null {
  if (!(err instanceof ApiError) || err.status !== 400) return null
  const body = err.body
  if (typeof body !== 'object' || body === null || !('errors' in body)) return null
  const list = (body as { errors: unknown }).errors
  if (!Array.isArray(list)) return null
  const out: BuilderFieldError[] = []
  for (const e of list) {
    if (typeof e === 'object' && e !== null && typeof (e as BuilderFieldError).field === 'string') {
      out.push({ field: (e as BuilderFieldError).field, error: String((e as BuilderFieldError).error ?? '') })
    }
  }
  return out
}

export function isHashChanged(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409
}

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import type {
  AccountActionResult,
  AgentDetail,
  ShareLinkResult,
  SuppressionRule,
  WebfilterActionResult,
  WebfilterDomainAction,
  WebfilterOverview,
  WebfilterRequestsResponse,
  WebfilterScheduleWindow,
} from './types'

export const agentQueryKey = (agentId: string) => ['agent', agentId] as const

export function useAgentDetail(agentId: string) {
  return useQuery({
    queryKey: agentQueryKey(agentId),
    queryFn: () => api.get<AgentDetail>(`/api/agent/${encodeURIComponent(agentId)}`),
    enabled: Boolean(agentId),
  })
}

/** After ANY mutation below, re-pull telemetry rather than patch the cache —
 * the agent is the source of truth (notes/view-endpoint-map.md, Host). */
function useInvalidateAgent(agentId: string) {
  const queryClient = useQueryClient()
  return () => queryClient.invalidateQueries({ queryKey: agentQueryKey(agentId) })
}

/* ── Host action row ────────────────────────────────────────────────────── */

export function useRefreshAgent(agentId: string) {
  const invalidate = useInvalidateAgent(agentId)
  return useMutation({
    mutationFn: () => api.post<{ ok: boolean; stored: boolean; warning?: string }>(`/api/agent/${agentId}/refresh`),
    onSuccess: () => invalidate(),
  })
}

export function useRemoteHelp(agentId: string) {
  return useMutation({
    mutationFn: () => api.post<{ ok: boolean; note?: string | null }>(`/api/agent/${agentId}/remotehelp`),
  })
}

export function useUpdateAgent(agentId: string) {
  const invalidate = useInvalidateAgent(agentId)
  return useMutation({
    mutationFn: () => api.post<{ ok: boolean; version?: string }>(`/api/agents/${agentId}/update`),
    onSuccess: () => invalidate(),
  })
}

export function useSetChannel(agentId: string) {
  const invalidate = useInvalidateAgent(agentId)
  return useMutation({
    mutationFn: (channel: 'stable' | 'dev') =>
      api.put<{ ok: boolean }>(`/api/agent/${agentId}/channel`, { channel }),
    onSuccess: () => invalidate(),
  })
}

export function useRemoveAgent() {
  return useMutation({
    mutationFn: (agentId: string) => api.delete<{ ok?: boolean }>(`/api/agent/${agentId}`),
  })
}

/**
 * "Re-share" — mints a fresh, single-use installer link for a host that's
 * already enrolled. Body-based, no `{id}` in the path (`distribution.py::
 * share_link_by_name`): this host's own `agent_id` is sent as `name`, the
 * same request shape the Add-a-PC wizard uses to name a NEW host — re-using
 * an existing id is exactly the "onboard again" case that route already
 * supports. `min_role="operator"` server-side; not exposed to a `user`
 * principal in this UI (see `ActionRow`).
 */
export function useShareLink(agentId: string) {
  return useMutation({
    mutationFn: (os: 'windows' | 'linux') =>
      api.post<ShareLinkResult>('/api/agents/share-link', { name: agentId, os }),
  })
}

export function useCaptureScreenshot(agentId: string) {
  return useMutation({
    mutationFn: () => api.post<{ ok: boolean }>(`/api/agent/${agentId}/screenshot`),
  })
}

/* ── Web filter ─────────────────────────────────────────────────────────── */

export function useWebfilter(agentId: string, enabled: boolean) {
  return useQuery({
    queryKey: ['webfilter', agentId],
    queryFn: () => api.get<WebfilterOverview>(`/api/agent/${agentId}/webfilter`),
    enabled,
  })
}

function useInvalidateWebfilter(agentId: string) {
  const queryClient = useQueryClient()
  return () => queryClient.invalidateQueries({ queryKey: ['webfilter', agentId] })
}

export function useSetWebfilterConfig(agentId: string) {
  const invalidate = useInvalidateWebfilter(agentId)
  return useMutation({
    mutationFn: (
      patch: Partial<{
        enabled: boolean
        block_mode: boolean
        doh_policy: 'disable' | 'leave'
        /** The whole enabled set, not a delta — sending it also settles the
         * two legacy toggle columns server-side so the representations can't
         * drift (`WebFilterStore.set_config`). This is how a category,
         * including `adult`/`bypass`, is turned on or off from this view. */
        categories: string[]
      }>,
    ) => api.put<{ config: WebfilterOverview['config'] }>(`/api/agent/${agentId}/webfilter/config`, patch),
    onSuccess: () => invalidate(),
  })
}

export function useAddWebfilterDomain(agentId: string) {
  const invalidate = useInvalidateWebfilter(agentId)
  return useMutation({
    mutationFn: (body: { domain: string; action: WebfilterDomainAction; category?: string | null }) =>
      api.post<{ domain: unknown; custom: unknown }>(`/api/agent/${agentId}/webfilter/domains`, body),
    onSuccess: () => invalidate(),
  })
}

export function useRemoveWebfilterDomain(agentId: string) {
  const invalidate = useInvalidateWebfilter(agentId)
  return useMutation({
    mutationFn: (domain: string) =>
      api.delete<{ ok: boolean }>(`/api/agent/${agentId}/webfilter/domains/${encodeURIComponent(domain)}`),
    onSuccess: () => invalidate(),
  })
}

export function useApplyWebfilter(agentId: string) {
  const invalidate = useInvalidateWebfilter(agentId)
  return useMutation({
    mutationFn: () => api.post<WebfilterActionResult>(`/api/agent/${agentId}/webfilter/apply`),
    onSuccess: () => invalidate(),
  })
}

/* ── Web filter schedule (ADR-0055) ─────────────────────────────────────── */

/** `overview.schedule` already carries the current windows + state, so
 * adding/removing a window invalidates the same query rather than a
 * dedicated one — there is no separate schedule cache to keep in step. */
export function useAddWebfilterWindow(agentId: string) {
  const invalidate = useInvalidateWebfilter(agentId)
  return useMutation({
    mutationFn: (body: {
      days: string[]
      start: string
      end: string
      categories: string[]
      label?: string
      timezone?: string
    }) => api.post<{ window: WebfilterScheduleWindow; schedule: WebfilterOverview['schedule'] }>(
      `/api/agent/${agentId}/webfilter/schedule`,
      body,
    ),
    onSuccess: () => invalidate(),
  })
}

export function useRemoveWebfilterWindow(agentId: string) {
  const invalidate = useInvalidateWebfilter(agentId)
  return useMutation({
    mutationFn: (windowId: string) =>
      api.delete<{ ok: boolean; removed: boolean; schedule: WebfilterOverview['schedule'] }>(
        `/api/agent/${agentId}/webfilter/schedule/${encodeURIComponent(windowId)}`,
      ),
    onSuccess: () => invalidate(),
  })
}

/* ── Web filter bypass requests ─────────────────────────────────────────── */

/** Open `web_filter`-category tickets for this host — a read over the
 * ticket store, not a second queue (ADR-0055). Granting one is the ordinary
 * `useAddWebfilterDomain` allow-domain call above; there is no separate
 * approve/deny mutation here. */
export function useWebfilterRequests(agentId: string, enabled: boolean) {
  return useQuery({
    queryKey: ['webfilter', agentId, 'requests'],
    queryFn: () => api.get<WebfilterRequestsResponse>(`/api/agent/${agentId}/webfilter/requests`),
    enabled,
  })
}

/* ── Local accounts ─────────────────────────────────────────────────────── */

export type AccountTool =
  | 'account_set_enabled'
  | 'account_set_admin'
  | 'account_set_logon_rights'
  | 'account_session_action'
  | 'account_delete'

export function useAccountAction(agentId: string) {
  const invalidate = useInvalidateAgent(agentId)
  return useMutation({
    mutationFn: ({ tool, args }: { tool: AccountTool; args: Record<string, unknown> }) =>
      api.post<AccountActionResult>(`/api/agent/${agentId}/accounts/${tool}`, args),
    // Re-pull telemetry so the checklist reflects the machine, not what the
    // operator hoped happened (notes/api-contract-actual.md §6).
    onSuccess: () => invalidate(),
  })
}

/* ── Reliability suppressions ───────────────────────────────────────────── */

export function useSuppressions() {
  return useQuery({
    queryKey: ['suppressions'],
    queryFn: () => api.get<{ rules: SuppressionRule[] }>('/api/reliability/suppressions'),
  })
}

export function useAddSuppression() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: { event_id: number; source?: string; agent_id?: string; note?: string }) =>
      api.post<{ rules: SuppressionRule[] }>('/api/reliability/suppressions', body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['suppressions'] }),
  })
}

export function useRemoveSuppression() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (ruleId: string) =>
      api.delete<{ ok: boolean; removed: boolean; rules: SuppressionRule[] }>(
        `/api/reliability/suppressions/${encodeURIComponent(ruleId)}`,
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['suppressions'] }),
  })
}

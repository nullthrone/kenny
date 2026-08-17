import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import type {
  AccountActionResult,
  AgentDetail,
  SuppressionRule,
  WebfilterActionResult,
  WebfilterDomainAction,
  WebfilterOverview,
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
    mutationFn: (patch: Partial<{ enabled: boolean; block_mode: boolean; use_external_adult: boolean; use_bypass_protection: boolean; doh_policy: 'disable' | 'leave' }>) =>
      api.put<{ config: WebfilterOverview['config'] }>(`/api/agent/${agentId}/webfilter/config`, patch),
    onSuccess: () => invalidate(),
  })
}

export function useAddWebfilterDomain(agentId: string) {
  const invalidate = useInvalidateWebfilter(agentId)
  return useMutation({
    mutationFn: (body: { domain: string; action: WebfilterDomainAction }) =>
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

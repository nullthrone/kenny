import { useQuery } from '@tanstack/react-query'
import { api } from './client'

/**
 * The AI features the server can switch on and off (ADR-0066), by the names
 * `GET /api/ai/status` uses. `aiFeatures.json` holds the same list; the server's
 * `kenny_server.ai.FEATURES` and this constant are both tested against it.
 */
export const AI_FEATURES = ['ask', 'recommend', 'forecast', 'classify', 'ticket_assistant', 'triage'] as const

export type AiFeature = (typeof AI_FEATURES)[number]

/** `GET /api/ai/status` — whether a key is set and which features may run. */
export interface AiStatus {
  configured: boolean
  source: 'db' | 'env' | 'none'
  features: Record<AiFeature, boolean>
}

export const AI_STATUS_KEY = ['ai', 'status'] as const

export function useAiStatus() {
  return useQuery({
    queryKey: AI_STATUS_KEY,
    queryFn: () => api.get<AiStatus>('/api/ai/status'),
    staleTime: 30_000,
  })
}

/**
 * Whether `feature` may run. False until the status is known, so a switched-off
 * feature never flashes a control that would answer 503.
 */
export function useAiFeature(feature: AiFeature): boolean {
  return useAiStatus().data?.features?.[feature] === true
}

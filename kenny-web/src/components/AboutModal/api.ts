import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import type { About, ChangelogResponse } from '../../api/types'

/**
 * Two of the three reads behind the About dialog. Split out so the Shell's
 * version line and the dialog itself share one cache entry per endpoint: opening
 * the dialog costs no second `/api/about` request. The third, `useAgentBinary`,
 * moved to `api/agentBinary.ts` once Fleet and the Add-a-PC wizard needed it too
 * — the About dialog is one of its readers, not its owner.
 *
 * All three sit at the server's authenticated floor (`guard(...)` with no
 * `min_role`), so there is no role gating to mirror here — About is reachable
 * for a scoped `user` exactly as it is for a superuser.
 */

/**
 * NOT gated on the dialog being open: the sidebar's version line reads it on
 * every page.
 *
 * The one place `staleTime` is deliberately raised above the app-wide 0
 * (`api/queryClient.ts`). That default exists so a newly-mounted view refetches
 * live fleet data on navigation; `/api/about` is a process constant —
 * `__version__`, `PROTOCOL_VERSION`, the configured repo — that cannot change
 * without a server restart, which ends the session anyway. Refetching it per
 * navigation would be a request per view change for a value that never moves.
 * This is not licence to raise `staleTime` on fleet data.
 */
export function useAbout() {
  return useQuery({
    queryKey: ['about'],
    queryFn: () => api.get<About>('/api/about'),
    staleTime: Infinity,
  })
}

/**
 * Best-effort, like the legacy dashboard's `.catch(() => ({ releases: [] }))`:
 * a GitHub outage must never stop the dialog showing the version rows.
 *
 * `staleTime` matches the server's own `changelog.CACHE_TTL_S`, and `retry` is
 * off because the server already degrades on its own — it serves its last good
 * cache and reports the upstream failure in the payload's `ok`/`error`, so a
 * client retry only delays the degraded state the user should see. Read that
 * pair: a 200 here says this API worked, not that GitHub did.
 */
export function useChangelog(enabled: boolean) {
  return useQuery({
    queryKey: ['changelog'],
    queryFn: () => api.get<ChangelogResponse>('/api/changelog'),
    enabled,
    staleTime: 5 * 60_000,
    retry: false,
  })
}


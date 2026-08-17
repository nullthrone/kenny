import { QueryClient } from '@tanstack/react-query'

/**
 * The old dashboard does ZERO polling (notes/api-contract-actual.md §6):
 * grep confirms no `setInterval` anywhere. Every "refresh" is either a
 * navigation (React Router mounting a new view's queries) or an explicit
 * refetch fired right after a mutation succeeds. This client is configured
 * to match that, not to "improve" on it:
 *
 * - `refetchInterval` stays unset (no polling — must not be added later).
 * - `refetchOnWindowFocus`/`refetchOnReconnect` are off: the old app never
 *   refetched on focus/reconnect, only on navigation, so turning these on
 *   would be new background network chatter that didn't exist before.
 * - `staleTime: 0` (the default) is what makes "refetch on navigation"
 *   work at all: a query considered stale refetches when a newly-mounted
 *   view subscribes to it, which is the React Query equivalent of the old
 *   app's per-tab-entry fetch.
 * - Mutations don't get any special defaults here; each mutation hook is
 *   expected to `invalidateQueries`/`refetch` the views it affects on
 *   success, mirroring the old app's explicit refresh-after-mutation calls
 *   — there is no optimistic-update layer.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchInterval: false,
      refetchOnWindowFocus: false,
      refetchOnReconnect: false,
      staleTime: 0,
      retry: 1,
    },
  },
})

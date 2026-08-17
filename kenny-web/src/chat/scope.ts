/**
 * Derives the host to scope a freshly-opened drawer to, from the current
 * URL. The app is a `HashRouter` (`src/App.tsx`) — the route lives in
 * `location.hash`, not `location.pathname` — so a host page is
 * `#/fleet/<host>` (never bare `#/fleet`, which is the fleet list).
 */
const HOST_ROUTE = /^#\/fleet\/([^/]+)$/

export function hostFromHash(hash: string): string {
  const match = HOST_ROUTE.exec(hash)
  return match ? decodeURIComponent(match[1]) : ''
}

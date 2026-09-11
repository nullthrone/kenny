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

/**
 * The ticket a freshly-opened drawer belongs to, from the same hash. A ticket
 * is `#/inbox/ticket/<id>`; `#/tickets/<id>` is the legacy path the router
 * still redirects from, and links kenny itself writes
 * (`ticket_assistant.py`) still use it, so both are read here.
 *
 * A ticket target is not a second kind of host scope. The host is the
 * ticket's own frozen `agent_id` and nothing said in the conversation can
 * move it (ADR-0038); what the ticket changes is *which gate* the turn runs
 * under — `TicketPolicy`, not the copilot's confirm-everything gate
 * (ADR-0050).
 */
const TICKET_ROUTE = /^#\/(?:inbox\/ticket|tickets)\/([^/?]+)/

export function ticketFromHash(hash: string): string {
  const match = TICKET_ROUTE.exec(hash)
  return match ? decodeURIComponent(match[1]) : ''
}

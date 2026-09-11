/**
 * How a ticket-scoped chat turn tells the ticket page that something landed.
 *
 * The drawer lives in `Shell` and the ticket page is a route below it: there
 * is no provider path between them, exactly as `views/host/askKenny.ts`
 * already found for the other direction. A window event is the seam, and it
 * carries only the ticket id — the page refetches from the server rather than
 * being handed state, because the durable record of a turn is the ticket's
 * own timeline, not anything the drawer holds.
 */
export const TICKET_TURN_EVENT = 'kenny:ticket-turn'

export function announceTicketTurn(ticketId: string): void {
  window.dispatchEvent(new CustomEvent(TICKET_TURN_EVENT, { detail: { ticketId } }))
}

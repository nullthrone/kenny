/**
 * The one place that turns `POST /api/approvals/{id}`'s `{resumed}` into
 * words. Used by both the Inbox row's inline gate and the ticket page's —
 * a `resumed === false` response must never read as plain success (the
 * decision was recorded; kenny just could not act on it automatically),
 * so this is deliberately not a simple "approved"/"denied" toast.
 */
export function decisionMessage(approve: boolean, resumed: boolean): string {
  const verb = approve ? 'Approved' : 'Denied'
  if (resumed) return `${verb}. Kenny is continuing.`
  return `${verb} — recorded, but kenny could not continue this ticket automatically.`
}

/**
 * Turns a `{ok:false, error, message?}` refusal from an account/webfilter
 * mutation into plain, factual copy. `disabled` and `blocked` are expected
 * refusals, not server faults (`kenny_server/webui/__init__.py::api_account_action`,
 * `api_webfilter_apply`) — Nullthrone voice states what happened, no apology.
 *
 * `list_too_large` is the over-cap state (`ListTooLargeError`, ADR-0055):
 * `count`/`cap` come back alongside `message` so this can state the number
 * and the cap without re-parsing the exception text. The overview's own
 * `oversize` banner is the primary surface for this state — this path
 * covers the apply button being clicked anyway before the overview refetches.
 */
export function describeActionError(error: string, message?: string, count?: number, cap?: number): string {
  switch (error) {
    case 'disabled':
      return 'Remote control is switched off at that machine. Monitoring continues.'
    case 'blocked':
      return message || 'The agent refused this on its own — a self-protection guard, not a server error.'
    case 'unsupported':
      return message || 'This action is not available on this host.'
    case 'list_too_large':
      return count !== undefined && cap !== undefined
        ? `The effective list is ${count.toLocaleString()} domains, over the ${cap.toLocaleString()}-domain cap. Not applied — monitoring continues. Turn off a category below to bring it under the cap.`
        : message || 'The effective list is too large to apply.'
    default:
      return message || error
  }
}

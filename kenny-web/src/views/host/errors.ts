/**
 * Turns a `{ok:false, error, message?}` refusal from an account/webfilter
 * mutation into plain, factual copy. `disabled` and `blocked` are expected
 * refusals, not server faults (`kenny_server/webui/__init__.py::api_account_action`,
 * `api_webfilter_apply`) — Nullthrone voice states what happened, no apology.
 */
export function describeActionError(error: string, message?: string): string {
  switch (error) {
    case 'disabled':
      return 'Remote control is switched off at that machine. Monitoring continues.'
    case 'blocked':
      return message || 'The agent refused this on its own — a self-protection guard, not a server error.'
    case 'unsupported':
      return message || 'This action is not available on this host.'
    default:
      return message || error
  }
}

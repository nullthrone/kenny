import { chatStore } from '../../chat/chatStore'

export const ASK_KENNY_OPEN_EVENT = 'kenny:ask-kenny-open'

/**
 * Hands a remediation prompt to the Ask Kenny chat drawer — what the section
 * modal's "FIX VIA ASK KENNY" button does with the recommendation stream's
 * `remediation.prompt`.
 *
 * `chatStore` (`src/chat/chatStore.ts`) is a module-level singleton, not tied
 * to whether the drawer is mounted — calling it here genuinely scopes the
 * conversation to this host and starts the turn, exactly like the drawer's
 * own composer would. The one piece this can't reach is forcing the drawer's
 * *visible* open/close — that's `chatOpen`, a plain `useState` local to
 * `Shell.tsx` (out of this view's ownership, no prop/context path in from
 * here) — so if the drawer is currently closed, the turn still runs, but the
 * operator only sees it once they open the drawer themselves (⌘K or the
 * header button).
 *
 * MOUNT POINT for whoever owns Shell: listen for
 * `window.addEventListener(ASK_KENNY_OPEN_EVENT, () => setChatOpen(true))`
 * to make this open the drawer immediately instead of leaving the turn
 * running invisibly.
 */
export function askKenny(prompt: string, agentId: string): void {
  chatStore.openForScope(agentId)
  void chatStore.sendMessage(prompt)
  window.dispatchEvent(new CustomEvent(ASK_KENNY_OPEN_EVENT))
}

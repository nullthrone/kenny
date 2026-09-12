import { useEffect, useSyncExternalStore } from 'react'
import { chatStore } from './chatStore'
import type { ChatSessionState } from './types'

export interface ChatSession {
  state: ChatSessionState
  sendMessage: typeof chatStore.sendMessage
  resolveGate: typeof chatStore.resolveGate
  createFromDraft: typeof chatStore.createFromDraft
  dismissDraft: typeof chatStore.dismissDraft
  stop: typeof chatStore.stop
  startNew: () => void
  loadConversation: typeof chatStore.loadConversation
  listHistory: typeof chatStore.listHistory
  deleteConversation: typeof chatStore.deleteConversation
}

/**
 * Subscribes the component to the singleton chat session (see chatStore.ts
 * for why this isn't plain `useState`) and, on mount, tells it which scope
 * this open of the drawer belongs to.
 *
 * `ticketId` wins over `agentId` when both are readable from the route: a
 * ticket already carries a frozen host, and the ticket's own gate is the
 * point of being on it (ADR-0050). The ticket's *details* are not read here —
 * the ticket page binds those (`chatStore.openForTicket`), which is why this
 * only has to notice that the drawer is no longer on the ticket it was on.
 */
export function useChatSession(agentId: string, ticketId = ''): ChatSession {
  const state = useSyncExternalStore(chatStore.subscribe, chatStore.getState)

  // Runs once per mount — the drawer remounts fresh on every open (Shell
  // fully unmounts it on close), so this correctly captures "the scope this
  // open of the drawer was invoked with" without re-firing on unrelated
  // re-renders. Deliberately not depending on its arguments.
  useEffect(() => {
    // On a ticket route, do nothing: the ticket page binds the target
    // (`openForTicket`), and falling back to a host scope here would quietly
    // put the turn on the copilot's endpoint and the copilot's gate while the
    // reader believed they were talking about the ticket. The drawer renders
    // an unbound ticket route as "not ready", never as fleet chat.
    if (!ticketId) chatStore.openForScope(agentId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return {
    state,
    sendMessage: chatStore.sendMessage,
    resolveGate: chatStore.resolveGate,
    createFromDraft: chatStore.createFromDraft,
    dismissDraft: chatStore.dismissDraft,
    stop: chatStore.stop,
    startNew: () => chatStore.reset(agentId),
    loadConversation: chatStore.loadConversation,
    listHistory: chatStore.listHistory,
    deleteConversation: chatStore.deleteConversation,
  }
}

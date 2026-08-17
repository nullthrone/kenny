import { useEffect, useSyncExternalStore } from 'react'
import { chatStore } from './chatStore'
import type { ChatSessionState } from './types'

export interface ChatSession {
  state: ChatSessionState
  sendMessage: typeof chatStore.sendMessage
  resolveGate: typeof chatStore.resolveGate
  stop: typeof chatStore.stop
  startNew: () => void
  loadConversation: typeof chatStore.loadConversation
  listHistory: typeof chatStore.listHistory
  deleteConversation: typeof chatStore.deleteConversation
}

/**
 * Subscribes the component to the singleton chat session (see chatStore.ts
 * for why this isn't plain `useState`) and, on mount, tells it which host
 * scope this open of the drawer belongs to.
 */
export function useChatSession(agentId: string): ChatSession {
  const state = useSyncExternalStore(chatStore.subscribe, chatStore.getState)

  // Runs once per mount — the drawer remounts fresh on every open (Shell
  // fully unmounts it on close), so this correctly captures "the scope this
  // open of the drawer was invoked with" without re-firing on unrelated
  // re-renders. Deliberately not depending on `agentId`.
  useEffect(() => {
    chatStore.openForScope(agentId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return {
    state,
    sendMessage: chatStore.sendMessage,
    resolveGate: chatStore.resolveGate,
    stop: chatStore.stop,
    startNew: () => chatStore.reset(agentId),
    loadConversation: chatStore.loadConversation,
    listHistory: chatStore.listHistory,
    deleteConversation: chatStore.deleteConversation,
  }
}

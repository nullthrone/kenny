import { useEffect, useSyncExternalStore } from 'react'
import { chatStore } from './chatStore'
import type { ChatSessionState } from './types'

export interface ChatSession {
  state: ChatSessionState
  sendMessage: (message: string) => Promise<void>
  resolveGate: (approve: boolean) => Promise<void>
  stop: () => void
  startNew: () => void
  loadConversation: (id: string) => Promise<void>
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
  // re-renders.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    chatStore.openForScope(agentId)
  }, [])

  return {
    state,
    sendMessage: (message: string) => chatStore.sendMessage(message),
    resolveGate: (approve: boolean) => chatStore.resolveGate(approve),
    stop: () => chatStore.stop(),
    startNew: () => chatStore.reset(agentId),
    loadConversation: (id: string) => chatStore.loadConversation(id),
    listHistory: chatStore.listHistory.bind(chatStore),
    deleteConversation: chatStore.deleteConversation.bind(chatStore),
  }
}

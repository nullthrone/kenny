import { useState } from 'react'
import { hostFromHash } from '../../chat/scope'
import { useChatSession } from '../../chat/useChatSession'
import { Plus, ScrollText, ICON_STROKE_WIDTH } from '../icons'
import Composer from './Composer'
import HistoryPanel from './HistoryPanel'
import PendingGateModal from './PendingGateModal'
import Transcript from './Transcript'
import styles from './AskKennyDrawer.module.css'

/**
 * Mounted by Shell inside the drawer chrome's placeholder region (the
 * header — title, ⌘K hint on the trigger button, close ✕ — and the
 * backdrop are Shell's, out of this component's reach; see
 * PendingGateModal's doc comment for why that's fine for the gate).
 *
 * Captured ONCE per mount, not tracked live: the drawer fully unmounts on
 * close (Shell's `{chatOpen && (...)}`), so a fresh mount is exactly "the
 * drawer was just opened" — the right moment to read the current host page
 * out of the URL and scope the conversation to it. Session state itself
 * lives outside this component (`chatStore`), so closing and reopening the
 * drawer does not lose the transcript or an unresolved gate.
 */
export default function AskKennyDrawer() {
  const [agentId] = useState(() => hostFromHash(window.location.hash))
  const chat = useChatSession(agentId)
  const [view, setView] = useState<'transcript' | 'history'>('transcript')

  const { state } = chat
  const gateOpen = state.pendingGate !== null

  return (
    <div className={styles.root}>
      <div className={styles.scopeRow}>
        <span className={styles.scopeChip}>scope: {state.agentId || 'fleet'}</span>
        <div className={styles.scopeActions}>
          <button
            type="button"
            className={`${styles.iconButton}${view === 'history' ? ` ${styles.iconButtonActive}` : ''}`}
            onClick={() => setView((v) => (v === 'history' ? 'transcript' : 'history'))}
            disabled={gateOpen}
            aria-pressed={view === 'history'}
            title="History"
          >
            <ScrollText width={15} height={15} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          </button>
          <button
            type="button"
            className={styles.iconButton}
            onClick={() => {
              chat.startNew()
              setView('transcript')
            }}
            disabled={gateOpen}
            title="New conversation"
          >
            <Plus width={15} height={15} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          </button>
        </div>
      </div>

      {view === 'history' ? (
        <HistoryPanel
          listHistory={chat.listHistory}
          deleteConversation={chat.deleteConversation}
          onSelect={(id) => {
            void chat.loadConversation(id)
            setView('transcript')
          }}
        />
      ) : (
        <>
          <Transcript items={state.items} />
          <Composer
            gateLocked={gateOpen}
            streaming={state.streaming}
            onSend={(message) => void chat.sendMessage(message)}
            onStop={chat.stop}
          />
        </>
      )}

      {state.pendingGate && (
        <PendingGateModal
          gate={state.pendingGate}
          onApprove={() => void chat.resolveGate(true)}
          onDeny={() => void chat.resolveGate(false)}
          busy={state.deciding}
        />
      )}
    </div>
  )
}

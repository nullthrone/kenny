import { useState } from 'react'
import { hostFromHash, ticketFromHash } from '../../chat/scope'
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
 * drawer was just opened" — the right moment to read the current page out of
 * the URL and scope the conversation to it. Session state itself lives
 * outside this component (`chatStore`), so closing and reopening the drawer
 * does not lose the transcript or an unresolved gate.
 *
 * **Two contexts, one surface.** On a host page this is the fleet copilot.
 * On a ticket it is that ticket's own chat (ADR-0050): the same transcript
 * and the same composer, posting to the ticket's endpoint, under the
 * ticket's gate, against the ticket's frozen host. Three things change with
 * it, and each is visible rather than inferred — the scope chip names the
 * ticket, there is no conversation history to browse because the ticket's
 * timeline *is* its history, and a gate is not decided here.
 */
export default function AskKennyDrawer() {
  const [route] = useState(() => ({
    agentId: hostFromHash(window.location.hash),
    ticketId: ticketFromHash(window.location.hash),
  }))
  const chat = useChatSession(route.agentId, route.ticketId)
  const [view, setView] = useState<'transcript' | 'history'>('transcript')

  const { state } = chat
  const gateOpen = state.pendingGate !== null
  const ticket = state.ticket
  // A ticket route whose target the ticket page has not bound yet. Never
  // rendered as fleet chat: sending a ticket's question down the copilot's
  // endpoint would run it under the wrong gate against no ticket at all.
  const notReady = !!route.ticketId && ticket?.id !== route.ticketId

  const unavailable = ticket
    ? !ticket.assistantAvailable
      ? 'The AI assistant is not configured on this server.'
      : !ticket.agentId
        ? 'This ticket has no target machine.'
        : ticket.blockedOnApproval
          ? 'Waiting on the decision on this ticket…'
          : undefined
    : undefined

  if (notReady) {
    return (
      <div className={styles.root}>
        <div className={styles.scopeRow}>
          <span className={styles.scopeChip}>opening this ticket…</span>
        </div>
      </div>
    )
  }

  return (
    <div className={styles.root}>
      <div className={styles.scopeRow}>
        <span className={styles.scopeChip}>
          {ticket ? `ticket #${ticket.number} · ${ticket.agentId || 'no host'}` : `scope: ${state.agentId || 'fleet'}`}
        </span>
        <div className={styles.scopeActions}>
          {/* A ticket's history is its timeline, on the page behind this
              drawer — there is no separate conversation to list or to start
              a new one of. */}
          {!ticket && (
            <>
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
            </>
          )}
        </div>
      </div>

      {view === 'history' && !ticket ? (
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
          {ticket && gateOpen && (
            // The gate is the ticket's, not this drawer's: durable, decided
            // beside the frozen call it would run, and free to wait for a
            // different operator than whoever has this open (ADR-0050).
            // Offering a second CONFIRM here would be offering the decision
            // without the evidence it is a decision about.
            <p className={styles.gateNotice}>
              Kenny needs a decision. It is on the ticket, next to the call it would run —
              close this drawer to answer it.
            </p>
          )}
          <Composer
            gateLocked={gateOpen}
            streaming={state.streaming}
            unavailable={unavailable}
            offerDiscordMirror={!!ticket?.discordThread}
            onSend={(message, mirrorToDiscord) => void chat.sendMessage(message, mirrorToDiscord)}
            onStop={chat.stop}
          />
        </>
      )}

      {!ticket && state.pendingGate && (
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

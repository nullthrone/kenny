import { useState } from 'react'
import { hostFromHash, ticketFromHash } from '../../chat/scope'
import { useChatSession } from '../../chat/useChatSession'
import { Plus, ScrollText, ICON_STROKE_WIDTH } from '../icons'
import GateCard from '../GateCard/GateCard'
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
 * ticket's gate, against the ticket's frozen host. Two things change with it,
 * and each is visible rather than inferred — the scope chip names the ticket,
 * and there is no conversation history to browse because the ticket's timeline
 * *is* its history.
 *
 * **Every decision kenny needs is made here**, on either surface. The copilot's
 * transient gate is a modal (its only exits are CONFIRM & RUN and CANCEL); a
 * ticket's gate is durable, so it sits inline and closing the drawer leaves it
 * open for later or for somebody else. What the two have in common is the part
 * that matters: the frozen call and its arguments are on the card being
 * decided, and there is exactly one place in the console where that decision
 * can be made.
 */
export default function AskKennyDrawer() {
  const [route] = useState(() => ({
    agentId: hostFromHash(window.location.hash),
    ticketId: ticketFromHash(window.location.hash),
  }))
  const chat = useChatSession(route.agentId, route.ticketId)
  const [view, setView] = useState<'transcript' | 'history'>('transcript')

  const { state, createFromDraft, dismissDraft } = chat
  const gateOpen = state.pendingGate !== null
  const ticket = state.ticket
  // A ticket route whose target the ticket page has not bound yet. Never
  // rendered as fleet chat: sending a ticket's question down the copilot's
  // endpoint would run it under the wrong gate against no ticket at all.
  const notReady = !!route.ticketId && ticket?.id !== route.ticketId

  const ticketGate = ticket?.gate ?? null
  const unavailable = ticket
    ? !ticket.assistantAvailable
      ? 'The AI assistant is not configured on this server.'
      : !ticket.agentId
        ? 'This ticket has no target machine.'
        : ticketGate
          ? 'Answer the decision above before asking anything else.'
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
          <Transcript
            items={state.items}
            openThinkingId={state.openThinkingId}
            pendingGateItemId={ticketGate ? state.pendingGate?.itemId ?? null : null}
            onCreateDraft={createFromDraft}
            onDismissDraft={dismissDraft}
          />
          {ticketGate && (
            // Inline rather than modal: this gate is durable. It may be
            // answered now, or minutes from now, or by a different operator on
            // another screen — closing the drawer is a legitimate "not yet",
            // and the ticket goes on saying it is waiting.
            <GateCard
              className={styles.ticketGate}
              tool={ticketGate.tool}
              args={ticketGate.args}
              agentId={ticketGate.agentId}
              toolClass={ticketGate.toolClass}
              approveLabel="CONFIRM & RUN"
              denyLabel="CANCEL"
              onApprove={() => void chat.resolveGate(true)}
              onDeny={() => void chat.resolveGate(false)}
              busy={state.deciding}
            />
          )}
          <Composer
            gateLocked={gateOpen || !!ticketGate}
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

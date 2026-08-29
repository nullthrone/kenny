import { useEffect, useRef, useState, useDeferredValue } from 'react'
import { streamChatEvents } from '../../api/sse'
import type { ChatEvent } from '../../api/types'
import Modal from '../../components/Modal/Modal'
import { ArrowUp, ICON_STROKE_WIDTH } from '../../components/icons'
import { composerKeyAction, readEnterToSend, writeEnterToSend } from '../../preferences'
import ApprovalGate, { type DecisionOutcome } from './ApprovalGate'
import type { TicketApproval } from './types'
import Markdown from '../../components/Markdown/Markdown'
import styles from './TicketChat.module.css'

interface StreamChip {
  key: string
  tool: string
  ok: boolean
}

export interface TicketChatProps {
  ticketId: string
  discordThread: boolean
  assistantAvailable: boolean
  /** `ticket.blocked_on === 'approval'` — server truth, not local stream state. Disables the composer even after the gate modal below is dismissed. */
  blockedOnApproval: boolean
  /** The ticket's one open gate, if any — shared with the page's own durable inline gate so both read one fetch. */
  openApproval: TicketApproval | undefined
  /** A `pending` event arrived mid-turn — the approval row is already durable server-side; go fetch it. */
  onNeedsApprovalRefetch: () => void
  /** A turn ended — reload the ticket + events so the timeline (not this transient panel) is authoritative. */
  onTurnDone: () => void
  onDecided: (outcome: DecisionOutcome) => void
}

/**
 * The ticket's own Ask-kenny composer, `POST /api/tickets/{id}/chat/stream`
 * (SSE). While a turn streams, tool runs/replies show in a transient panel
 * above the input — cleared the moment `done` fires, because the durable
 * timeline below is what's authoritative afterward, not this buffer.
 */
export default function TicketChat({
  ticketId,
  discordThread,
  assistantAvailable,
  blockedOnApproval,
  openApproval,
  onNeedsApprovalRefetch,
  onTurnDone,
  onDecided,
}: TicketChatProps) {
  const [message, setMessage] = useState('')
  const [mirrorToDiscord, setMirrorToDiscord] = useState(false)
  // Read once at mount; the checkbox below is the only thing that changes it,
  // and it writes both this state and the stored preference.
  const [enterSends, setEnterSends] = useState(readEnterToSend)
  const [streaming, setStreaming] = useState(false)
  const [streamText, setStreamText] = useState('')
  const [chips, setChips] = useState<StreamChip[]>([])
  const [error, setError] = useState<string | null>(null)
  const [awaitingGate, setAwaitingGate] = useState(false)
  const [gateModalOpen, setGateModalOpen] = useState(false)
  const abortRef = useRef<AbortController | null>(null)

  // The refetch `onNeedsApprovalRefetch` kicked off lands here — once the
  // caller's `openApproval` shows up, promote the wait into the real modal.
  useEffect(() => {
    if (awaitingGate && openApproval) {
      setGateModalOpen(true)
      setAwaitingGate(false)
    }
  }, [awaitingGate, openApproval])

  async function send() {
    const text = message.trim()
    if (!text || streaming) return
    setMessage('')
    setStreamText('')
    setChips([])
    setError(null)
    setStreaming(true)
    const controller = new AbortController()
    abortRef.current = controller

    try {
      for await (const event of streamChatEvents(
        `/api/tickets/${ticketId}/chat/stream`,
        { message: text, mirror_to_discord: mirrorToDiscord },
        { signal: controller.signal },
      )) {
        handleEvent(event)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setStreaming(false)
      abortRef.current = null
    }
  }

  function handleEvent(event: ChatEvent) {
    switch (event.type) {
      case 'text_delta':
        setStreamText((prev) => prev + event.text)
        return
      case 'tool_result':
        setChips((prev) => [...prev, { key: `${prev.length}-${event.tool}`, tool: event.tool, ok: event.ok }])
        return
      case 'denied':
        setChips((prev) => [...prev, { key: `${prev.length}-${event.tool}`, tool: `denied ${event.tool}`, ok: false }])
        return
      case 'pending':
        setAwaitingGate(true)
        onNeedsApprovalRefetch()
        return
      case 'error':
        setError(event.error)
        return
      case 'done':
        setStreamText('')
        setChips([])
        onTurnDone()
        return
      default:
        return
    }
  }

  function stop() {
    abortRef.current?.abort()
  }

  // The renderer takes the whole accumulated buffer on every delta — a delta can
  // split a markdown token, and only re-parsing the complete buffer recovers
  // from that. Deferring it keeps a fast token stream off the critical path;
  // `Markdown` is memoised, so an unchanged buffer costs nothing.
  const deferredStreamText = useDeferredValue(streamText)

  const disabled = streaming || blockedOnApproval || !assistantAvailable

  return (
    <div className={styles.wrap}>
      {(streaming || streamText || chips.length > 0 || error) && (
        <div className={styles.stream}>
          <span className={styles.streamLabel}>{streaming ? 'KENNY IS WORKING…' : 'LAST TURN'}</span>
          {chips.map((chip) => (
            <span key={chip.key} className={`${styles.chip}${chip.ok ? '' : ` ${styles.chipFail}`}`}>
              {chip.tool}
            </span>
          ))}
          {deferredStreamText && <Markdown className={styles.streamText} text={deferredStreamText} />}
          {error && <div className={styles.errorText}>{error}</div>}
        </div>
      )}

      <div className={`${styles.row} kc-actions`}>
        <input
          className={styles.input}
          placeholder={
            blockedOnApproval
              ? 'Waiting on the confirmation above…'
              : !assistantAvailable
                ? 'The AI assistant is not configured on this server.'
                : 'Ask kenny about this ticket…'
          }
          value={message}
          disabled={disabled}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={(e) => {
            if (e.key !== 'Enter') return
            if (composerKeyAction(e, enterSends) === 'send') {
              e.preventDefault()
              void send()
            }
          }}
        />
        {streaming ? (
          <button type="button" className={styles.send} onClick={stop} aria-label="Stop">
            STOP
          </button>
        ) : (
          <button type="button" className={styles.send} disabled={disabled} onClick={() => void send()} aria-label="Send">
            <ArrowUp width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          </button>
        )}
      </div>
      {discordThread && !disabled && (
        <label className={styles.mirrorRow}>
          <input
            type="checkbox"
            className={styles.checkbox}
            checked={mirrorToDiscord}
            onChange={(e) => setMirrorToDiscord(e.target.checked)}
          />
          Also send to the Discord thread
        </label>
      )}
      {/* Off by default, so Enter inserts a newline and Cmd/Ctrl+Enter sends — a
          ticket reply is often several lines, and Enter sending them one at a time
          is the failure this preference exists to prevent. Remembered per browser
          (`kenny-enter-send`), and the same preference governs the Ask kenny drawer. */}
      <label className={styles.mirrorRow}>
        <input
          type="checkbox"
          className={styles.checkbox}
          checked={enterSends}
          onChange={(e) => {
            setEnterSends(e.target.checked)
            writeEnterToSend(e.target.checked)
          }}
        />
        Enter to send
      </label>
      {!assistantAvailable && <div className={styles.disabledNote}>The AI assistant is not configured on this server.</div>}

      <Modal
        open={gateModalOpen && !!openApproval}
        onClose={() => setGateModalOpen(false)}
        dismissible
        labelledBy="chat-gate-title"
        width={520}
      >
        <div style={{ padding: 20 }}>
          <div id="chat-gate-title" style={{ fontFamily: 'var(--font-display)', fontSize: 12, letterSpacing: 'var(--track-caps)', marginBottom: 14 }}>
            KENNY NEEDS A DECISION
          </div>
          {openApproval && (
            <ApprovalGate
              approvalId={openApproval.id}
              tool={openApproval.tool}
              args={openApproval.args}
              agentId={openApproval.agent_id}
              toolClass={openApproval.tool_class}
              onDecideLater={() => setGateModalOpen(false)}
              onDecided={(outcome) => {
                setGateModalOpen(false)
                onDecided(outcome)
              }}
            />
          )}
        </div>
      </Modal>
    </div>
  )
}

import { useState, type KeyboardEvent } from 'react'
import { composerKeyAction } from '../../preferences'
import { ArrowUp, ICON_STROKE_WIDTH } from '../icons'
import styles from './Composer.module.css'

export interface ComposerProps {
  /** True while the confirm gate is open — the composer is fully locked, matching the design's
   * "Waiting on the confirmation above…" state (non-negotiable #3). */
  gateLocked: boolean
  /** True while a turn is streaming (no gate). Input stays usable-looking but disabled; a Stop
   * button replaces the disabled send affordance. */
  streaming: boolean
  /** Why the composer cannot be used at all, when that is the case — shown as the placeholder. */
  unavailable?: string
  /** Offered only when the ticket this conversation belongs to has a thread to mirror into. */
  offerDiscordMirror?: boolean
  onSend: (message: string, mirrorToDiscord: boolean) => void
  onStop: () => void
}

/** Wires `kenny-enter-send` (src/preferences.ts): off by default, Enter inserts a newline;
 * opted in, Enter sends and Shift+Enter is what inserts a newline instead. */
export default function Composer({
  gateLocked,
  streaming,
  unavailable,
  offerDiscordMirror,
  onSend,
  onStop,
}: ComposerProps) {
  const [value, setValue] = useState('')
  const [mirror, setMirror] = useState(false)
  const locked = gateLocked || streaming || !!unavailable

  function submit() {
    const trimmed = value.trim()
    if (!trimmed || locked) return
    onSend(trimmed, mirror)
    setValue('')
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key !== 'Enter') return
    if (composerKeyAction(e) === 'send') {
      e.preventDefault()
      submit()
    }
  }

  return (
    <>
      <div className={styles.root}>
        <textarea
          className={styles.input}
          rows={1}
          placeholder={
            unavailable ?? (gateLocked ? 'Waiting on the confirmation above…' : 'Ask kenny…')
          }
          value={value}
          disabled={locked}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
          aria-label="Message kenny"
        />
        {streaming ? (
          <button type="button" className={styles.stop} onClick={onStop}>
            STOP
          </button>
        ) : (
          <button
            type="button"
            className={styles.send}
            disabled={locked || value.trim().length === 0}
            onClick={submit}
            aria-label="Send"
          >
            <ArrowUp width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          </button>
        )}
      </div>
      {offerDiscordMirror && !locked && (
        // Off by default, deliberately: kenny's answer is on the ticket
        // either way, and putting it in the family's thread is a separate
        // choice made per answer.
        <label className={styles.mirror}>
          <input type="checkbox" checked={mirror} onChange={(e) => setMirror(e.target.checked)} />
          Also send to the Discord thread
        </label>
      )}
    </>
  )
}

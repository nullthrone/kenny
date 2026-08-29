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
  onSend: (message: string) => void
  onStop: () => void
}

/** Wires `kenny-enter-send` (src/preferences.ts): off by default, Enter inserts a newline;
 * opted in, Enter sends and Shift+Enter is what inserts a newline instead. */
export default function Composer({ gateLocked, streaming, onSend, onStop }: ComposerProps) {
  const [value, setValue] = useState('')
  const locked = gateLocked || streaming

  function submit() {
    const trimmed = value.trim()
    if (!trimmed || locked) return
    onSend(trimmed)
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
    <div className={styles.root}>
      <textarea
        className={styles.input}
        rows={1}
        placeholder={gateLocked ? 'Waiting on the confirmation above…' : 'Ask kenny…'}
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
  )
}

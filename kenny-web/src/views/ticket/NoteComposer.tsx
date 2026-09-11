import { useState, type KeyboardEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api } from '../../api/client'
import { composerKeyAction } from '../../preferences'
import styles from './NoteComposer.module.css'

export interface NoteComposerProps {
  ticketId: string
  requesterLabel?: string
  onPosted: () => void
}

/**
 * `POST /api/tickets/{id}/note` — operator-only, and the ticket's only inline
 * composer.
 *
 * A note is written *onto* a ticket; asking kenny is a conversation *about*
 * one, and that lives in the Ask kenny drawer bound to this ticket
 * (ADR-0050). Two permanent input boxes under one timeline made the reader
 * choose between them before knowing which they wanted.
 *
 * A textarea, not a single line: a note can be a paragraph, and Enter follows
 * the same `kenny-enter-send` preference every other composer does
 * (`src/preferences.ts`) instead of being the one field with its own rule.
 */
export default function NoteComposer({ ticketId, requesterLabel, onPosted }: NoteComposerProps) {
  const [text, setText] = useState('')

  const post = useMutation({
    mutationFn: (summary: string) => api.post(`/api/tickets/${ticketId}/note`, { summary }),
    onSuccess: () => {
      setText('')
      onPosted()
    },
  })

  function submit() {
    const trimmed = text.trim()
    if (!trimmed || post.isPending) return
    post.mutate(trimmed)
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key !== 'Enter') return
    if (composerKeyAction(e) === 'send') {
      e.preventDefault()
      submit()
    }
  }

  return (
    <div className={styles.row}>
      <textarea
        className={styles.input}
        rows={1}
        placeholder={requesterLabel ? `Add a note — visible to ${requesterLabel} in Discord…` : 'Add a note…'}
        value={text}
        aria-label="Add a note"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
      />
      <button
        type="button"
        className={styles.post}
        disabled={!text.trim() || post.isPending}
        onClick={submit}
      >
        POST
      </button>
    </div>
  )
}

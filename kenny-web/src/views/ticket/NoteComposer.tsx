import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api } from '../../api/client'
import styles from './NoteComposer.module.css'

export interface NoteComposerProps {
  ticketId: string
  requesterLabel?: string
  onPosted: () => void
}

/** `POST /api/tickets/{id}/note` — operator-only, mirrors the design's plain input + POST button. */
export default function NoteComposer({ ticketId, requesterLabel, onPosted }: NoteComposerProps) {
  const [text, setText] = useState('')

  const post = useMutation({
    mutationFn: (summary: string) => api.post(`/api/tickets/${ticketId}/note`, { summary }),
    onSuccess: () => {
      setText('')
      onPosted()
    },
  })

  return (
    <div className={styles.row}>
      <input
        className={styles.input}
        placeholder={requesterLabel ? `Add a note — visible to ${requesterLabel} in Discord…` : 'Add a note…'}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && text.trim() && !post.isPending) post.mutate(text.trim())
        }}
      />
      <button
        type="button"
        className={styles.post}
        disabled={!text.trim() || post.isPending}
        onClick={() => post.mutate(text.trim())}
      >
        POST
      </button>
    </div>
  )
}

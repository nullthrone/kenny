import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api, ApiError } from '../../api/client'
import Modal from '../../components/Modal/Modal'
import { X, ICON_STROKE_WIDTH } from '../../components/icons'
import shared from './shared.module.css'

export interface PasswordModalProps {
  open: boolean
  onClose: () => void
}

/** Profile → Password → CHANGE. `POST /api/me/password`, requires the current password. */
export default function PasswordModal({ open, onClose }: PasswordModalProps) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')

  const mutation = useMutation({
    mutationFn: () => api.post<{ ok: boolean }>('/api/me/password', { current_password: current, new_password: next }),
    onSuccess: () => {
      setCurrent('')
      setNext('')
      setConfirm('')
      onClose()
    },
  })

  function handleClose() {
    mutation.reset()
    setCurrent('')
    setNext('')
    setConfirm('')
    onClose()
  }

  const mismatch = confirm.length > 0 && next !== confirm
  const canSubmit = current.length > 0 && next.length > 0 && !mismatch

  return (
    <Modal open={open} onClose={handleClose} labelledBy="password-modal-title" width={420}>
      <div id="password-modal-title" className={shared.header}>
        Change password
        <button type="button" className={shared.closeBtn} onClick={handleClose} aria-label="Close">
          <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} />
        </button>
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          if (canSubmit) mutation.mutate()
        }}
      >
        <div className={shared.body}>
          {mutation.isError && (
            <div className={shared.errorBox}>
              {mutation.error instanceof ApiError ? mutation.error.message : 'Could not change the password. Try again.'}
            </div>
          )}
          <label className={shared.field}>
            <span className={shared.fieldLabel}>CURRENT PASSWORD</span>
            <input
              type="password"
              className={shared.input}
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              autoComplete="current-password"
              required
            />
          </label>
          <label className={shared.field}>
            <span className={shared.fieldLabel}>NEW PASSWORD</span>
            <input
              type="password"
              className={shared.input}
              value={next}
              onChange={(e) => setNext(e.target.value)}
              autoComplete="new-password"
              required
            />
          </label>
          <label className={shared.field}>
            <span className={shared.fieldLabel}>CONFIRM NEW PASSWORD</span>
            <input
              type="password"
              className={shared.input}
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              autoComplete="new-password"
              required
            />
            {mismatch && <p className={shared.help}>The passwords do not match.</p>}
          </label>
          <p className={shared.help}>Changing your password signs out every other session — this browser stays signed in.</p>
        </div>
        <div className={shared.footer}>
          <button type="submit" className={shared.btnPrimary} disabled={!canSubmit || mutation.isPending}>
            {mutation.isPending ? 'SAVING…' : 'SAVE PASSWORD'}
          </button>
          <button type="button" className={shared.btn} onClick={handleClose}>
            CANCEL
          </button>
        </div>
      </form>
    </Modal>
  )
}

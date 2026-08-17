import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../api/client'
import Modal from '../../components/Modal/Modal'
import { X, ICON_STROKE_WIDTH } from '../../components/icons'
import type { AvatarsResponse, ProfileMe } from './types'
import shared from './shared.module.css'
import styles from './EditProfileModal.module.css'

export interface EditProfileModalProps {
  open: boolean
  onClose: () => void
  me: ProfileMe
}

/**
 * Profile identity card → EDIT PROFILE. `PATCH /api/me` only accepts
 * `{email, avatar}` — the account's username is not user-editable
 * server-side, so this modal does not offer a name field.
 */
export default function EditProfileModal({ open, onClose, me }: EditProfileModalProps) {
  const queryClient = useQueryClient()
  const [email, setEmail] = useState(me.email ?? '')
  const [avatar, setAvatar] = useState(me.avatar)

  useEffect(() => {
    if (open) {
      setEmail(me.email ?? '')
      setAvatar(me.avatar)
    }
  }, [open, me.email, me.avatar])

  const avatars = useQuery({
    queryKey: ['avatars'],
    queryFn: () => api.get<AvatarsResponse>('/api/avatars'),
    enabled: open,
    staleTime: Infinity,
  })

  const save = useMutation({
    mutationFn: () => api.patch<ProfileMe>('/api/me', { email: email.trim() || null, avatar }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['me'] })
      onClose()
    },
  })

  return (
    <Modal open={open} onClose={onClose} labelledBy="edit-profile-title" width={460}>
      <div id="edit-profile-title" className={shared.header}>
        Edit profile
        <button type="button" className={shared.closeBtn} onClick={onClose} aria-label="Close">
          <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} />
        </button>
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          save.mutate()
        }}
      >
        <div className={shared.body}>
          {save.isError && (
            <div className={shared.errorBox}>
              {save.error instanceof ApiError ? save.error.message : 'Could not save the profile. Try again.'}
            </div>
          )}
          <label className={shared.field}>
            <span className={shared.fieldLabel}>EMAIL</span>
            <input type="email" className={shared.input} value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>
          <div className={shared.field}>
            <span className={shared.fieldLabel}>AVATAR</span>
            {avatars.isLoading && <p className={shared.help}>Loading avatars…</p>}
            {avatars.data && (
              <div className={styles.avatarGrid}>
                {avatars.data.avatars.map((a) => (
                  <button
                    key={a}
                    type="button"
                    className={`${styles.avatarBtn}${avatar === a ? ` ${styles.selected}` : ''}`}
                    onClick={() => setAvatar(a)}
                    aria-pressed={avatar === a}
                    aria-label={a.replace(/-/g, ' ')}
                  >
                    <img src={`/assets/${a}.png`} alt="" />
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
        <div className={shared.footer}>
          <button type="submit" className={shared.btnPrimary} disabled={save.isPending}>
            {save.isPending ? 'SAVING…' : 'SAVE PROFILE'}
          </button>
          <button type="button" className={shared.btn} onClick={onClose}>
            CANCEL
          </button>
        </div>
      </form>
    </Modal>
  )
}

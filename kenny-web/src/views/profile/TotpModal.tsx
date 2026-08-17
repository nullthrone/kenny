import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../api/client'
import Modal from '../../components/Modal/Modal'
import { X, ICON_STROKE_WIDTH } from '../../components/icons'
import type { TotpSetup } from './types'
import shared from './shared.module.css'

export interface TotpModalProps {
  open: boolean
  onClose: () => void
  /** Current state from `/api/me` — decides enable vs. disable flow. */
  enabled: boolean
}

/**
 * Profile → Two-factor auth → MANAGE.
 *
 * Enable is two calls (`POST /api/me/totp` mints `{secret, uri}`, then
 * `PUT /api/me/totp {secret, code}` verifies and turns it on). Disable is
 * one call, password-gated (`DELETE /api/me/totp {password}`).
 */
export default function TotpModal({ open, onClose, enabled }: TotpModalProps) {
  const queryClient = useQueryClient()
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')

  const setup = useQuery({
    queryKey: ['me', 'totp-setup'],
    queryFn: () => api.post<TotpSetup>('/api/me/totp'),
    enabled: open && !enabled,
    staleTime: Infinity,
  })

  const verify = useMutation({
    mutationFn: () => {
      if (!setup.data) throw new Error('no pending setup')
      return api.put<{ ok: boolean; totp_enabled: boolean }>('/api/me/totp', { secret: setup.data.secret, code })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['me'] })
      handleClose()
    },
  })

  const disable = useMutation({
    mutationFn: () => api.delete<{ ok: boolean; totp_enabled: boolean }>('/api/me/totp', { password }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['me'] })
      handleClose()
    },
  })

  useEffect(() => {
    if (!open) {
      setCode('')
      setPassword('')
      verify.reset()
      disable.reset()
      queryClient.removeQueries({ queryKey: ['me', 'totp-setup'] })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  function handleClose() {
    setCode('')
    setPassword('')
    onClose()
  }

  return (
    <Modal open={open} onClose={handleClose} labelledBy="totp-modal-title" width={460}>
      <div id="totp-modal-title" className={shared.header}>
        Two-factor authentication
        <button type="button" className={shared.closeBtn} onClick={handleClose} aria-label="Close">
          <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} />
        </button>
      </div>

      {enabled ? (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            disable.mutate()
          }}
        >
          <div className={shared.body}>
            <p className={shared.help}>Two-factor is currently enabled on this account. Disabling it requires your password.</p>
            {disable.isError && (
              <div className={shared.errorBox}>
                {disable.error instanceof ApiError ? disable.error.message : 'Could not disable two-factor. Try again.'}
              </div>
            )}
            <label className={shared.field}>
              <span className={shared.fieldLabel}>PASSWORD</span>
              <input
                type="password"
                className={shared.input}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
              />
            </label>
          </div>
          <div className={shared.footer}>
            <button type="submit" className={shared.btnDanger} disabled={!password || disable.isPending}>
              {disable.isPending ? 'DISABLING…' : 'DISABLE TWO-FACTOR'}
            </button>
            <button type="button" className={shared.btn} onClick={handleClose}>
              CANCEL
            </button>
          </div>
        </form>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            verify.mutate()
          }}
        >
          <div className={shared.body}>
            {setup.isLoading && <p className={shared.help}>Generating a secret…</p>}
            {setup.isError && <div className={shared.errorBox}>Could not start two-factor setup. Try again.</div>}
            {setup.data && (
              <>
                <p className={shared.help}>Add this to your authenticator app, then enter the 6-digit code it shows.</p>
                <label className={shared.field}>
                  <span className={shared.fieldLabel}>SETUP URI</span>
                  <input type="text" className={`${shared.input} ${shared.mono}`} value={setup.data.uri} readOnly />
                </label>
                <label className={shared.field}>
                  <span className={shared.fieldLabel}>SECRET (MANUAL ENTRY)</span>
                  <input type="text" className={`${shared.input} ${shared.mono}`} value={setup.data.secret} readOnly />
                </label>
                <label className={shared.field}>
                  <span className={shared.fieldLabel}>6-DIGIT CODE</span>
                  <input
                    type="text"
                    inputMode="numeric"
                    pattern="[0-9]*"
                    className={`${shared.input} ${shared.mono}`}
                    value={code}
                    onChange={(e) => setCode(e.target.value)}
                    maxLength={6}
                    autoComplete="one-time-code"
                    required
                  />
                </label>
                {verify.isError && (
                  <div className={shared.errorBox}>
                    {verify.error instanceof ApiError ? verify.error.message : 'That code did not verify. Try again.'}
                  </div>
                )}
              </>
            )}
          </div>
          <div className={shared.footer}>
            <button type="submit" className={shared.btnPrimary} disabled={!setup.data || code.length < 6 || verify.isPending}>
              {verify.isPending ? 'VERIFYING…' : 'VERIFY & ENABLE'}
            </button>
            <button type="button" className={shared.btn} onClick={handleClose}>
              CANCEL
            </button>
          </div>
        </form>
      )}
    </Modal>
  )
}

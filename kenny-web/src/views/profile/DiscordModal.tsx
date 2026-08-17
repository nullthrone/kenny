import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../api/client'
import Modal from '../../components/Modal/Modal'
import { X, ICON_STROKE_WIDTH } from '../../components/icons'
import type { DiscordMeStatus, DiscordUnlinkResponse } from './types'
import shared from './shared.module.css'

export interface DiscordModalProps {
  open: boolean
  onClose: () => void
}

function formatDate(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

/**
 * Profile → Discord → MANAGE. `GET /api/me/discord` returns the caller's own
 * binding(s) — only the raw Discord account id (the snowflake), never a
 * display name: the only name kenny ever sees lives on the mutable,
 * unverified `/link` claim, never on the identity itself (ADR-0044), so none
 * is invented here.
 *
 * Linking is deliberately not self-service (ADR-0044) — a chat platform's
 * identity assertion carries no proof of possession, so an operator has to
 * confirm the `/link` claim in Admin. Unlinking only takes privilege away,
 * so it needs no operator step: `DELETE /api/me/discord` is a plain user
 * action here.
 */
export default function DiscordModal({ open, onClose }: DiscordModalProps) {
  const queryClient = useQueryClient()

  const status = useQuery({
    queryKey: ['me', 'discord'],
    queryFn: () => api.get<DiscordMeStatus>('/api/me/discord'),
    enabled: open,
  })

  const unlink = useMutation({
    mutationFn: () => api.delete<DiscordUnlinkResponse>('/api/me/discord'),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['me', 'discord'] }),
  })

  function handleClose() {
    unlink.reset()
    onClose()
  }

  function handleUnlink() {
    if (!window.confirm('Unlink Discord from your account? To relink, run /link again in Discord and have an operator confirm it.')) return
    unlink.mutate()
  }

  const bindings = status.data?.bindings ?? []

  return (
    <Modal open={open} onClose={handleClose} labelledBy="discord-modal-title" width={480}>
      <div id="discord-modal-title" className={shared.header}>
        Discord
        <button type="button" className={shared.closeBtn} onClick={handleClose} aria-label="Close">
          <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} />
        </button>
      </div>
      <div className={shared.body}>
        {status.isLoading && <p className={shared.help}>Loading…</p>}
        {status.isError && <div className={shared.errorBox}>Could not load Discord status. Reload to try again.</div>}

        {status.data && !status.data.linked && (
          <p className={shared.help}>
            Not linked. Run <code className={shared.mono}>/link</code> in Discord, then ask an operator to confirm the claim
            in Admin — linking a Discord account is not self-service.
          </p>
        )}

        {status.data && status.data.linked && (
          <>
            <div className={shared.list}>
              {bindings.map((b) => (
                <div key={`${b.discord_user_id}:${b.guild_id}`} className={shared.listRow}>
                  <div className={shared.listMeta}>
                    <div className={`${shared.listLabel} ${shared.mono}`}>{b.discord_user_id}</div>
                    <div className={shared.listSub}>
                      guild {b.guild_id} · linked {formatDate(b.linked_at)} via {b.linked_via}
                    </div>
                  </div>
                </div>
              ))}
            </div>
            <p className={shared.help}>{status.data.note}</p>

            {unlink.isError && (
              <div className={shared.errorBox}>
                {unlink.error instanceof ApiError ? unlink.error.message : 'Could not unlink. Try again.'}
              </div>
            )}

            <button type="button" className={shared.btnDanger} onClick={handleUnlink} disabled={unlink.isPending}>
              {unlink.isPending ? 'UNLINKING…' : 'UNLINK DISCORD'}
            </button>
          </>
        )}
      </div>
      <div className={shared.footer}>
        <button type="button" className={shared.btn} onClick={handleClose}>
          CLOSE
        </button>
      </div>
    </Modal>
  )
}

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../../api/client'
import Modal from '../../../components/Modal/Modal'
import { X, ICON_STROKE_WIDTH } from '../../../components/icons'
import type { FleetResponse, Role } from '../../../api/types'
import type { AdminUser } from '../types'
import shared from '../shared.module.css'

export interface CreateUserModalProps {
  open: boolean
  onClose: () => void
}

const ROLES: Role[] = ['user', 'operator', 'superuser']

/** Admin → Users → ADD USER. `POST /api/users`; `hosts` is sent only when the role is `user`. */
export default function CreateUserModal({ open, onClose }: CreateUserModalProps) {
  const queryClient = useQueryClient()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<Role>('user')
  const [hosts, setHosts] = useState<string[]>([])

  const fleet = useQuery({ queryKey: ['fleet'], queryFn: () => api.get<FleetResponse>('/api/fleet'), enabled: open })

  const create = useMutation({
    mutationFn: () =>
      api.post<AdminUser>('/api/users', {
        username: username.trim(),
        password,
        role,
        ...(role === 'user' ? { hosts } : {}),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'users'] })
      reset()
      onClose()
    },
  })

  function reset() {
    setUsername('')
    setPassword('')
    setRole('user')
    setHosts([])
  }

  function handleClose() {
    reset()
    create.reset()
    onClose()
  }

  function toggleHost(id: string) {
    setHosts((h) => (h.includes(id) ? h.filter((x) => x !== id) : [...h, id]))
  }

  return (
    <Modal open={open} onClose={handleClose} labelledBy="create-user-title" width={460}>
      <div id="create-user-title" className={shared.cardTitle} style={{ padding: '20px 22px 0' }}>
        Add user
        <button
          type="button"
          onClick={handleClose}
          style={{ float: 'right', background: 'transparent', border: 'none', color: 'var(--text-muted)' }}
          aria-label="Close"
        >
          <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} />
        </button>
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          create.mutate()
        }}
      >
        <div style={{ padding: '16px 22px', display: 'flex', flexDirection: 'column', gap: 14, maxHeight: '60vh', overflowY: 'auto' }}>
          {create.isError && (
            <div className={shared.errorBox}>{create.error instanceof ApiError ? create.error.message : 'Could not create the user.'}</div>
          )}
          <label className={shared.field}>
            <span className={shared.fieldLabel}>USERNAME</span>
            <input type="text" className={shared.input} value={username} onChange={(e) => setUsername(e.target.value)} required />
          </label>
          <label className={shared.field}>
            <span className={shared.fieldLabel}>PASSWORD</span>
            <input type="password" className={shared.input} value={password} onChange={(e) => setPassword(e.target.value)} required />
          </label>
          <label className={shared.field}>
            <span className={shared.fieldLabel}>ROLE</span>
            <select className={shared.input} value={role} onChange={(e) => setRole(e.target.value as Role)}>
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </label>
          {role === 'user' && (
            <div className={shared.field}>
              <span className={shared.fieldLabel}>HOST SCOPE</span>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {fleet.data?.agents.map((a) => (
                  <label key={a.agent_id} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 'var(--text-sm)' }}>
                    <input type="checkbox" checked={hosts.includes(a.agent_id)} onChange={() => toggleHost(a.agent_id)} />
                    {a.agent_id}
                  </label>
                ))}
              </div>
            </div>
          )}
        </div>
        <div className={shared.actions} style={{ padding: '0 22px 20px' }}>
          <button type="submit" className={shared.btnPrimary} disabled={!username.trim() || !password || create.isPending}>
            {create.isPending ? 'CREATING…' : 'CREATE USER'}
          </button>
          <button type="button" className={shared.btn} onClick={handleClose}>
            CANCEL
          </button>
        </div>
      </form>
    </Modal>
  )
}

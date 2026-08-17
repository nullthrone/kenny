import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../../api/client'
import EmptyState from '../../../components/EmptyState/EmptyState'
import type { AdminUser } from '../types'
import type { ProfileMe } from '../../profile/types'
import shared from '../shared.module.css'
import CreateUserModal from './CreateUserModal'
import UserDetailModal from './UserDetailModal'

/** Admin → Users. Superuser-only (the section itself is hidden otherwise by `AdminView`). */
export default function UsersSection() {
  const [createOpen, setCreateOpen] = useState(false)
  const [detailId, setDetailId] = useState<number | null>(null)

  const me = useQuery({ queryKey: ['me'], queryFn: () => api.get<ProfileMe>('/api/me') })
  const users = useQuery({ queryKey: ['admin', 'users'], queryFn: () => api.get<{ users: AdminUser[] }>('/api/users') })

  if (users.isLoading) return <div className={shared.loading}>Loading…</div>
  if (users.isError) return <EmptyState title="Could not load users" message="Something went wrong. Reload to try again." />
  if (!users.data) return null

  return (
    <div>
      <div className={shared.table}>
        {users.data.users.map((u) => (
          <div key={u.id} className={shared.tableRow}>
            <div className={shared.tableMeta}>
              <div className={shared.tableLabel}>{u.username}</div>
              <div className={shared.tableSub}>
                {u.role} · {u.email || 'no email'}
                {u.disabled ? ' · disabled' : ''}
                {u.totp_enabled ? ' · 2FA on' : ''}
              </div>
            </div>
            <button type="button" className={shared.btnSmall} onClick={() => setDetailId(u.id)}>
              MANAGE
            </button>
          </div>
        ))}
      </div>
      <div className={shared.actions}>
        <button type="button" className={shared.btnPrimary} onClick={() => setCreateOpen(true)}>
          ADD USER
        </button>
      </div>

      <CreateUserModal open={createOpen} onClose={() => setCreateOpen(false)} />
      <UserDetailModal userId={detailId} onClose={() => setDetailId(null)} ownId={me.data?.id ?? null} />
    </div>
  )
}

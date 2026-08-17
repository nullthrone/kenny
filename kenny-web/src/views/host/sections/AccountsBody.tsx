import { useState } from 'react'
import Modal from '../../../components/Modal/Modal'
import { X, ICON_STROKE_WIDTH } from '../../../components/icons'
import type { LocalAccount, LocalAccountsSection } from '../types'
import { describeActionError } from '../errors'
import { formatRelativeTime } from '../format'
import { useAccountAction, type AccountTool } from '../api'
import styles from './AccountsBody.module.css'

export interface AccountsBodyProps {
  agentId: string
  accounts: LocalAccountsSection
}

/** One row's worth of pending-action + error state, keyed by principal so
 * one account's in-flight action doesn't disable another's controls. */
function useAccountActionState(agentId: string) {
  const mutation = useAccountAction(agentId)
  const [pendingPrincipal, setPendingPrincipal] = useState<string | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})

  function run(principal: string, tool: AccountTool, args: Record<string, unknown>) {
    setPendingPrincipal(principal)
    setErrors((e) => ({ ...e, [principal]: '' }))
    mutation.mutate(
      { tool, args: { principal, ...args } },
      {
        onSuccess: (r) => {
          setPendingPrincipal(null)
          if (!r.ok) setErrors((e) => ({ ...e, [principal]: describeActionError(r.error, r.message) }))
        },
        onError: (err) => {
          setPendingPrincipal(null)
          setErrors((e) => ({ ...e, [principal]: err instanceof Error ? err.message : 'Could not complete that action.' }))
        },
      },
    )
  }

  return { run, pendingPrincipal, errors }
}

function AccountCard({
  agentId,
  account,
  run,
  pending,
  error,
}: {
  agentId: string
  account: LocalAccount
  run: (principal: string, tool: AccountTool, args: Record<string, unknown>) => void
  pending: boolean
  error?: string
}) {
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [removeProfile, setRemoveProfile] = useState(false)
  const unsupported = account.unsupported ?? {}
  const deny = new Set(account.deny_logon ?? [])

  function toggleDeny(right: 'network' | 'remote_interactive') {
    const next = new Set(deny)
    if (next.has(right)) next.delete(right)
    else next.add(right)
    run(account.name, 'account_set_logon_rights', { deny: Array.from(next) })
  }

  return (
    <div className={styles.card}>
      <div className={styles.head}>
        <span className={styles.name}>{account.name}</span>
        {account.display && account.display !== account.name && <span className={styles.display}>{account.display}</span>}
        <span className={styles.kindChip}>{account.kind.toUpperCase()}</span>
        {account.builtin_admin && <span className={styles.kindChip}>BUILT-IN ADMIN</span>}
        {account.builtin_guest && <span className={styles.kindChip}>BUILT-IN GUEST</span>}
        <span className={styles.spacer} />
        <span className={styles.meta}>
          last logon {account.last_logon ? formatRelativeTime(account.last_logon) : 'never'}
        </span>
      </div>

      <div className={styles.controls}>
        <label className={styles.toggle}>
          <input
            type="checkbox"
            checked={account.enabled}
            disabled={pending || Boolean(unsupported.set_enabled)}
            onChange={(e) => run(account.name, 'account_set_enabled', { enabled: e.target.checked })}
          />
          enabled
        </label>
        <label className={styles.toggle}>
          <input
            type="checkbox"
            checked={account.is_admin}
            disabled={pending || Boolean(unsupported.set_admin)}
            onChange={(e) => run(account.name, 'account_set_admin', { admin: e.target.checked })}
          />
          administrator
        </label>
        <label className={styles.toggle}>
          <input
            type="checkbox"
            checked={deny.has('network')}
            disabled={pending || Boolean(unsupported.deny_network)}
            onChange={() => toggleDeny('network')}
          />
          deny network logon
        </label>
        <label className={styles.toggle}>
          <input
            type="checkbox"
            checked={deny.has('remote_interactive')}
            disabled={pending || Boolean(unsupported.deny_remote_interactive)}
            onChange={() => toggleDeny('remote_interactive')}
          />
          deny remote desktop
        </label>
        <button
          type="button"
          className={styles.actionButton}
          disabled={pending || Boolean(unsupported.session_lock)}
          onClick={() => run(account.name, 'account_session_action', { action: 'lock' })}
        >
          LOCK SESSION
        </button>
        <button
          type="button"
          className={styles.actionButton}
          disabled={pending || Boolean(unsupported.session_logoff)}
          onClick={() => run(account.name, 'account_session_action', { action: 'logoff' })}
        >
          LOG OFF
        </button>
        <button
          type="button"
          className={styles.removeButton}
          disabled={pending || Boolean(unsupported.delete)}
          onClick={() => setConfirmDelete(true)}
        >
          REMOVE
        </button>
      </div>

      {Object.keys(unsupported).length > 0 && (
        <div className={styles.unsupportedNote}>not available: {Object.keys(unsupported).join(', ')}</div>
      )}
      {error && <div className={styles.error}>{error}</div>}

      <Modal open={confirmDelete} onClose={() => setConfirmDelete(false)} labelledBy={`remove-${account.name}`} width={420}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '16px 20px', borderBottom: '1px solid var(--border-line)' }}>
          <span id={`remove-${account.name}`} style={{ fontFamily: 'var(--font-display)', fontSize: 12, letterSpacing: 'var(--track-caps)' }}>
            REMOVE {account.name.toUpperCase()}
          </span>
          <button
            type="button"
            onClick={() => setConfirmDelete(false)}
            style={{ background: 'none', border: 'none', color: 'var(--text-muted)', minWidth: 44, minHeight: 44, display: 'grid', placeItems: 'center' }}
          >
            <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          </button>
        </div>
        <div style={{ padding: 20 }}>
          <p style={{ fontSize: 'var(--text-sm)', color: 'var(--text-muted)', margin: '0 0 14px' }}>
            Removes the account entry on {agentId}. A Microsoft-account-backed entry is unlinked from this PC only —
            the cloud account is untouched.
          </p>
          <label className={styles.toggle} style={{ marginBottom: 18 }}>
            <input type="checkbox" checked={removeProfile} onChange={(e) => setRemoveProfile(e.target.checked)} />
            also delete the profile and home directory
          </label>
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, borderTop: '1px solid var(--border-line)', paddingTop: 14 }}>
            <button
              type="button"
              className={styles.actionButton}
              onClick={() => setConfirmDelete(false)}
            >
              CANCEL
            </button>
            <button
              type="button"
              className={styles.removeButton}
              onClick={() => {
                run(account.name, 'account_delete', { remove_profile: removeProfile })
                setConfirmDelete(false)
              }}
            >
              REMOVE ACCOUNT
            </button>
          </div>
        </div>
      </Modal>
    </div>
  )
}

/** Full-edit local-accounts section modal body. The account list is not a
 * dedicated GET — it's the `local_accounts` telemetry section, which is also
 * the tools' own inventory (docs/protocol.md, "local_accounts": "since v0.15
 * this section is also the inventory for the account_* governance tools"). */
export default function AccountsBody({ agentId, accounts }: AccountsBodyProps) {
  const { run, pendingPrincipal, errors } = useAccountActionState(agentId)
  const policy = accounts.password_policy

  return (
    <div>
      {policy && (
        <p className={styles.policy}>
          Password policy ({policy.applies_to.replace('_', ' ')}): min length {policy.min_length ?? '—'}, max age{' '}
          {policy.max_age_days ? `${policy.max_age_days} days` : 'none'}, lockout after {policy.lockout_threshold ?? '—'}{' '}
          attempts.
        </p>
      )}
      <div className={styles.eyebrow}>
        {accounts.count} ACCOUNT{accounts.count === 1 ? '' : 'S'} · {accounts.admins.length} ADMIN
        {accounts.admins.length === 1 ? '' : 'S'}
      </div>
      <div className={styles.list}>
        {accounts.accounts.map((a) => (
          <AccountCard
            key={a.name}
            agentId={agentId}
            account={a}
            run={run}
            pending={pendingPrincipal === a.name}
            error={errors[a.name]}
          />
        ))}
      </div>
    </div>
  )
}

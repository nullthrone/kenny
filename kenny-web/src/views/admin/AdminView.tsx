import { useMemo } from 'react'
import { Navigate, useParams } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import EmptyState from '../../components/EmptyState/EmptyState'
import type { ProfileMe } from '../profile/types'
import type { RawSettingsResponse } from './types'
import { mapSettingsGroups } from './settingsMap'
import AdminNav, { type AdminNavItem } from './AdminNav'
import GenericSettingsSection from './sections/GenericSettingsSection'
import AlertsSection from './sections/AlertsSection'
import AlarmRulesSection from './sections/AlarmRulesSection'
import BackupSection from './sections/BackupSection'
import UpdatesSection from './sections/UpdatesSection'
import DiscordSection from './sections/DiscordSection'
import UsersSection from './sections/UsersSection'
import ShellPolicySection from './sections/ShellPolicySection'
import styles from './AdminView.module.css'

/** Sections with no settings group of their own behind them. */
const SYNTHETIC_LABELS: Record<string, string> = {
  updates: 'Updates',
  'alarm-rules': 'Alarm rules',
  users: 'Users',
}

/**
 * Section slugs that moved, mapped to where they live now. `#/settings/:section`
 * redirects into `#/admin/:section` keeping the slug (`router/routes.tsx`), so an
 * old bookmark arrives here verbatim; resolving the alias here covers the
 * redirect and a hand-typed URL in one place. A slug whose section no longer
 * exists at all (the read-only environment groups) falls through to the first
 * section, like any other unknown one.
 */
const SLUG_ALIASES: Record<string, string> = {
  'ticket-rules': 'alarm-rules',
  'auto-ticket-rules': 'alarm-rules',
  'alerting-digest': 'alerts-notifications',
  'chat-ai': 'ai',
  'discord-tickets': 'discord',
  logging: 'system',
  'telemetry-limits': 'system',
}

/**
 * `#/admin/:section` — 220px section nav + a row list.
 *
 * **Everything here can be changed here.** `GET /api/settings` lists only
 * settings the dashboard can write (env-only ones are the server environment's
 * business and never appear), and the synthetic sections are all actions and
 * rules. What a section shows besides its settings is the state those settings
 * or actions act on — the Discord connection, the backup list, the rollout.
 *
 * What the nav holds depends on the role, because `GET /api/settings` is
 * superuser-only (`webui/__init__.py`). A superuser gets every catalog group
 * (`groups[].slug`, in the server's order) plus **Alarm rules** (right after the
 * alerts it governs) and **Users**.
 *
 * An operator gets **Updates** and **Alarm rules** — the two sections whose own
 * routes floor at `operator` (`/api/updates`, `/api/ticket-rules`,
 * `/api/reliability/suppressions`). Both render without `/api/settings` ever
 * being requested: asking for it would 403 and take the whole page down with it.
 */
export default function AdminView() {
  const { section: rawSection } = useParams<{ section?: string }>()
  const section = rawSection ? (SLUG_ALIASES[rawSection] ?? rawSection) : rawSection

  const me = useQuery({ queryKey: ['me'], queryFn: () => api.get<ProfileMe>('/api/me') })
  const isSuperuser = me.data?.role === 'superuser'
  const isOperator = me.data ? me.data.role !== 'user' : false

  const settings = useQuery({
    queryKey: ['settings'],
    queryFn: () => api.get<RawSettingsResponse>('/api/settings'),
    enabled: isSuperuser,
  })

  const groups = useMemo(() => (settings.data ? mapSettingsGroups(settings.data) : []), [settings.data])

  const navItems: AdminNavItem[] = useMemo(() => {
    if (!isOperator) return []
    const alarmRules = { key: 'alarm-rules', label: SYNTHETIC_LABELS['alarm-rules'] }
    if (!isSuperuser) {
      return [{ key: 'updates', label: SYNTHETIC_LABELS.updates }, alarmRules]
    }
    const items: AdminNavItem[] = []
    for (const g of groups) {
      items.push({ key: g.key, label: g.label })
      if (g.key === 'alerts-notifications') items.push(alarmRules)
    }
    if (!items.includes(alarmRules)) items.push(alarmRules)
    items.push({ key: 'users', label: SYNTHETIC_LABELS.users })
    return items
  }, [groups, isOperator, isSuperuser])

  if (me.isLoading || (isSuperuser && settings.isLoading)) {
    return (
      <div className={`kc-content kc-view ${styles.root}`}>
        <h1 className="kc-h1" style={{ fontFamily: 'var(--font-display)', fontWeight: 500, fontSize: 'var(--display-md)', margin: '0 0 24px' }}>
          Admin
        </h1>
        <div className={styles.loading}>Loading…</div>
      </div>
    )
  }

  // An unreadable identity is not the same as an insufficient one: falling
  // through to the message below would tell an operator their role is too low
  // when the truth is we never learned what it is.
  if (me.isError || !me.data) {
    return (
      <div className={`kc-content kc-view ${styles.root}`}>
        <h1 className="kc-h1" style={{ fontFamily: 'var(--font-display)', fontWeight: 500, fontSize: 'var(--display-md)', margin: '0 0 24px' }}>
          Admin
        </h1>
        <EmptyState title="Could not read your account" message="Something went wrong. Reload to try again." />
      </div>
    )
  }

  // A scoped `user` has no section here at all: every admin route floors at
  // `operator`. The nav item is hidden for them, so this is the hand-typed-URL
  // path — say so, rather than drawing a nav of sections that all 403.
  if (!isOperator) {
    return (
      <div className={`kc-content kc-view ${styles.root}`}>
        <h1 className="kc-h1" style={{ fontFamily: 'var(--font-display)', fontWeight: 500, fontSize: 'var(--display-md)', margin: '0 0 24px' }}>
          Admin
        </h1>
        <EmptyState
          title="Admin is for operators"
          message="Your account works its own tickets and the PCs assigned to it. Fleet administration needs an operator."
        />
      </div>
    )
  }

  if (isSuperuser && (settings.isError || !settings.data)) {
    return (
      <div className={`kc-content kc-view ${styles.root}`}>
        <h1 className="kc-h1" style={{ fontFamily: 'var(--font-display)', fontWeight: 500, fontSize: 'var(--display-md)', margin: '0 0 24px' }}>
          Admin
        </h1>
        <EmptyState title="Could not load the settings catalog" message="Something went wrong. Reload to try again." />
      </div>
    )
  }

  // Bare #/admin resolves to the first section this role can see — never an
  // invented placeholder slug. An aliased slug is rewritten to its canonical one
  // so the address bar and the nav highlight agree.
  if (!section) {
    const first = navItems[0]?.key ?? 'updates'
    return <Navigate to={`/admin/${first}`} replace />
  }
  if (rawSection && section !== rawSection) {
    return <Navigate to={`/admin/${section}`} replace />
  }

  // A deep link into a section this role cannot see (a `users` link without a
  // superuser session, or any catalog group as an operator) resolves to the first
  // section it can — the section is hidden entirely, not just its nav entry.
  if (!navItems.some((item) => item.key === section)) {
    const first = navItems[0]?.key
    if (first) return <Navigate to={`/admin/${first}`} replace />
  }

  const activeGroup = groups.find((g) => g.key === section)
  const rows = activeGroup?.rows ?? []
  const title = activeGroup?.label ?? SYNTHETIC_LABELS[section] ?? section

  return (
    <div className={`kc-content kc-view ${styles.root}`}>
      <h1 className="kc-h1" style={{ fontFamily: 'var(--font-display)', fontWeight: 500, fontSize: 'var(--display-md)', margin: '0 0 24px' }}>
        Admin
      </h1>
      <div className={`kc-adminwrap ${styles.wrap}`}>
        <AdminNav items={navItems} />
        <div>
          <div className={styles.sectionTitle}>{title.toUpperCase()}</div>
          {section === 'alerts-notifications' ? (
            <AlertsSection rows={rows} />
          ) : section === 'alarm-rules' ? (
            <AlarmRulesSection />
          ) : section === 'backup' ? (
            <BackupSection rows={rows} />
          ) : section === 'updates' ? (
            <UpdatesSection rows={rows} />
          ) : section === 'discord' ? (
            <DiscordSection rows={rows} />
          ) : section === 'shell-policy' ? (
            <ShellPolicySection rows={rows} />
          ) : section === 'users' ? (
            <UsersSection />
          ) : activeGroup ? (
            <GenericSettingsSection rows={rows} />
          ) : (
            <EmptyState title="Unknown section" message="This section does not exist. Pick one from the list on the left." />
          )}
        </div>
      </div>
    </div>
  )
}

import type { Role } from '../api/types'
import { SunMedium, Monitor, Inbox, ScrollText, Settings2, type LucideIcon } from './icons'

export type NavKey = 'today' | 'fleet' | 'inbox' | 'log' | 'admin'

export interface NavItemDef {
  key: NavKey
  label: string
  icon: LucideIcon
  href: string
  /** Route prefixes (besides `href` itself) that should also light this item up. */
  activePrefixes: string[]
  /** Lowest role the destination is usable for. Absent means every signed-in role. */
  minRole?: Role
}

/** Role hierarchy from `security.py`: superuser > operator > user. */
const ROLE_RANK: Record<Role, number> = { user: 0, operator: 1, superuser: 2 }

/**
 * The five sidebar/mobile-tab-bar nav items, shared by Shell and
 * MobileTabBar so the two surfaces can never drift.
 *
 * ADMIN points at bare `#/admin`, which `AdminView` resolves to the first
 * section the signed-in role can actually see. There is deliberately no
 * hardcoded slug here: real section slugs come from the server's settings
 * catalog (`config.py`'s `GROUP_ORDER` -> `group_slug`) and are not
 * enumerable client-side, so any literal written here would be a guess.
 *
 * ADMIN is operator+: every section behind it floors at `operator`
 * server-side, so a `user` following the link would only ever reach a 403.
 * LOG carries no `minRole` on purpose — `GET /api/log` floors at `user` and
 * narrows the rows to the caller's own hosts (`webui/__init__.py`
 * `api_log`), so a scoped user gets a real, useful view of their machines.
 */
export const NAV_ITEMS: NavItemDef[] = [
  { key: 'today', label: 'TODAY', icon: SunMedium, href: '/today', activePrefixes: [] },
  { key: 'fleet', label: 'FLEET', icon: Monitor, href: '/fleet', activePrefixes: [] },
  { key: 'inbox', label: 'INBOX', icon: Inbox, href: '/inbox', activePrefixes: [] },
  { key: 'log', label: 'LOG', icon: ScrollText, href: '/log', activePrefixes: ['/activity'] },
  { key: 'admin', label: 'ADMIN', icon: Settings2, href: '/admin', activePrefixes: ['/settings'], minRole: 'operator' },
]

/** The nav items a given role may reach. `null` (identity not loaded yet) shows the unprivileged set. */
export function navItemsFor(role: Role | null): NavItemDef[] {
  return NAV_ITEMS.filter((item) => !item.minRole || (role !== null && ROLE_RANK[role] >= ROLE_RANK[item.minRole]))
}

export function activeNavKey(pathname: string): NavKey | null {
  for (const item of NAV_ITEMS) {
    const prefixes = [item.href, ...item.activePrefixes]
    if (prefixes.some((p) => pathname === p || pathname.startsWith(`${p}/`))) return item.key
  }
  return null
}

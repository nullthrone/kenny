import { SunMedium, Monitor, Inbox, ScrollText, Settings2, type LucideIcon } from './icons'

export type NavKey = 'today' | 'fleet' | 'inbox' | 'log' | 'admin'

export interface NavItemDef {
  key: NavKey
  label: string
  icon: LucideIcon
  href: string
  /** Route prefixes (besides `key`'s own view) that should also light this item up — mirrors the prototype's `alsoActive` on FLEET (host detail counts as Fleet). */
  activePrefixes: string[]
}

/**
 * The five sidebar/mobile-tab-bar nav items, shared by Shell and
 * MobileTabBar so the two surfaces can never drift.
 *
 * The ADMIN href points at `/admin/general` — a PLACEHOLDER section slug.
 * `#/admin/:section` is the only admin route in the contract (no bare
 * `#/admin`), and real section slugs come from the server's dynamic
 * settings catalog (`AdminSection.key`, not enumerable here). Router.tsx
 * adds a matching `admin` → `admin/general` redirect so the link never
 * 404s; whoever builds the Admin view should replace both with the real
 * first-available-section slug once that's known.
 */
export const NAV_ITEMS: NavItemDef[] = [
  { key: 'today', label: 'TODAY', icon: SunMedium, href: '/today', activePrefixes: [] },
  { key: 'fleet', label: 'FLEET', icon: Monitor, href: '/fleet', activePrefixes: [] },
  { key: 'inbox', label: 'INBOX', icon: Inbox, href: '/inbox', activePrefixes: [] },
  { key: 'log', label: 'LOG', icon: ScrollText, href: '/log', activePrefixes: [] },
  { key: 'admin', label: 'ADMIN', icon: Settings2, href: '/admin/general', activePrefixes: ['/admin'] },
]

export function activeNavKey(pathname: string): NavKey | null {
  for (const item of NAV_ITEMS) {
    if (pathname === item.href || pathname.startsWith(`${item.href}/`)) return item.key
    if (item.key === 'admin' && pathname.startsWith('/admin')) return 'admin'
    if (pathname.startsWith(`/${item.key}`)) return item.key
  }
  return null
}

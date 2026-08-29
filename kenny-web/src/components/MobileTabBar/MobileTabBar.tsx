import { NavLink, useLocation } from 'react-router'
import type { Role } from '../../api/types'
import { activeNavKey, navItemsFor, type NavKey } from '../navItems'
import { ICON_STROKE_WIDTH } from '../icons'
import styles from './MobileTabBar.module.css'

export interface MobileTabBarProps {
  /** The signed-in role, so the bar shows exactly the destinations the sidebar does. */
  role: Role | null
  /** Per-destination badge text, keyed by nav key. Shown as a dot at this size. */
  navBadges?: Partial<Record<NavKey, string>>
}

/**
 * The fixed 5-tab bottom bar that replaces the sidebar below 760px
 * (prototype lines 444-452 / the `@media (max-width:760px)` block, lines
 * 30-49). Visibility is entirely driven by the global `.kc-mobilebar`
 * class — see that class's comment in src/styles/global.css.
 *
 * Rendered once by Shell, as a fixed-position sibling of the sidebar/main
 * — not per-view. Shell owns both the role and the badge counts and passes
 * them down, so the two nav surfaces always show the same destinations.
 */
export default function MobileTabBar({ role, navBadges }: MobileTabBarProps) {
  const location = useLocation()
  const active = activeNavKey(location.pathname)

  return (
    <nav className={`${styles.bar} kc-mobilebar`} aria-label="Sections (compact)">
      {navItemsFor(role).map((item) => {
        const isActive = active === item.key
        const Icon = item.icon
        const badge = navBadges?.[item.key]
        return (
          <NavLink
            key={item.key}
            to={item.href}
            className={styles.tab}
            style={{ color: isActive ? '#F4F2EC' : 'var(--ink-300)' }}
          >
            <Icon width={20} height={20} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
            <span className={styles.label}>{item.label}</span>
            {badge && <span className={styles.badgeDot} />}
          </NavLink>
        )
      })}
    </nav>
  )
}

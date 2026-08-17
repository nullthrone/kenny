import { NavLink, useLocation } from 'react-router'
import { NAV_ITEMS, activeNavKey } from '../navItems'
import { ICON_STROKE_WIDTH } from '../icons'
import styles from './MobileTabBar.module.css'

export interface MobileTabBarProps {
  navBadges?: Partial<Record<(typeof NAV_ITEMS)[number]['key'], string>>
}

/**
 * The fixed 5-tab bottom bar that replaces the sidebar below 760px
 * (prototype lines 444-452 / the `@media (max-width:760px)` block, lines
 * 30-49). Visibility is entirely driven by the global `.kc-mobilebar`
 * class — see that class's comment in src/styles/global.css.
 *
 * Rendered once by Shell, as a fixed-position sibling of the sidebar/main
 * — not per-view.
 */
export default function MobileTabBar({ navBadges }: MobileTabBarProps) {
  const location = useLocation()
  const active = activeNavKey(location.pathname)

  return (
    <nav className={`${styles.bar} kc-mobilebar`}>
      {NAV_ITEMS.map((item) => {
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

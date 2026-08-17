import { NavLink } from 'react-router'
import styles from './AdminNav.module.css'

export interface AdminNavItem {
  key: string
  label: string
}

export interface AdminNavProps {
  items: AdminNavItem[]
}

/** The 220px section nav (a horizontal scroller below 760px via the global `.kc-adminnav` rule). */
export default function AdminNav({ items }: AdminNavProps) {
  return (
    <nav className={`${styles.nav} kc-adminnav`}>
      {items.map((item) => (
        <NavLink
          key={item.key}
          to={`/admin/${item.key}`}
          className={({ isActive }) => `${styles.item}${isActive ? ` ${styles.active}` : ''}`}
        >
          {item.label.toUpperCase()}
        </NavLink>
      ))}
    </nav>
  )
}

import { Link } from 'react-router'
import type { InboxItem } from '../../api/types'
import PriorityBadge from '../../components/PriorityBadge/PriorityBadge'
import { formatAge, toRoutePath } from './age'
import styles from './InboxRow.module.css'

export interface InboxRowProps {
  item: InboxItem
}

/**
 * One hairline row: priority badge, title link, meta, age.
 *
 * A row waiting on an approval says so in its `meta` and offers no decision:
 * approving happens on the ticket, where the frozen call it would run is
 * shown. Deciding from a list, next to a title, is deciding without the
 * evidence.
 */
export default function InboxRow({ item }: InboxRowProps) {
  // Every row the server emits carries a route to its ticket. A row without
  // one is rendered as plain text rather than as a link to `''`, which reads
  // as clickable and then navigates back to the inbox.
  const route = toRoutePath(item.target)

  return (
    <div className={`${styles.row} kc-stagger-row`} data-shot="inbox-row">
      <PriorityBadge priority={item.priority} className={styles.badge} />
      <div className={`${styles.body} kc-cell`}>
        {route ? (
          <Link to={route} className={styles.title}>
            {item.title}
          </Link>
        ) : (
          <span className={styles.title}>{item.title}</span>
        )}
        <div className={styles.meta}>{item.meta}</div>
      </div>
      <span className={styles.age}>{formatAge(item.age_seconds)}</span>
    </div>
  )
}

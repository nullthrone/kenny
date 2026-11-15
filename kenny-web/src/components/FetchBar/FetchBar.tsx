import { useIsFetching } from '@tanstack/react-query'
import styles from './FetchBar.module.css'

/**
 * A 2px brass indeterminate bar along the bottom edge of the header while any
 * query is in flight. It fades in only after a short delay (see the module
 * CSS), so the fast background polls that keep the console live stay
 * invisible and only a fetch the operator could actually wait on shows.
 */
export default function FetchBar() {
  const fetching = useIsFetching()
  if (fetching === 0) return null
  return (
    <div className={styles.track} aria-hidden="true">
      <div className={styles.bar} />
    </div>
  )
}

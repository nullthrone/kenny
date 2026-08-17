import { useParams } from 'react-router'

/** Placeholder — the next wave fills this view in. Handles both #/inbox and #/inbox/:group. */
export default function Inbox() {
  const { group } = useParams<{ group?: string }>()
  return (
    <div className="kc-content kc-view">
      <h1 className="kc-h1">Inbox{group ? ` · ${group}` : ''}</h1>
    </div>
  )
}

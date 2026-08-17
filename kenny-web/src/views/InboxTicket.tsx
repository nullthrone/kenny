import { useParams } from 'react-router'

/** Placeholder — the next wave fills this view in. */
export default function InboxTicket() {
  const { id } = useParams<{ id: string }>()
  return (
    <div className="kc-content kc-view">
      <h1 className="kc-h1">Ticket #{id}</h1>
    </div>
  )
}

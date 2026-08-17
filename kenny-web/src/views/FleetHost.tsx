import { useParams } from 'react-router'

/** Placeholder — the next wave fills this view in. */
export default function FleetHost() {
  const { host } = useParams<{ host: string }>()
  return (
    <div className="kc-content kc-view">
      <h1 className="kc-h1">{host}</h1>
    </div>
  )
}

import { useParams } from 'react-router'

/** Placeholder — the next wave fills this view in. */
export default function Admin() {
  const { section } = useParams<{ section: string }>()
  return (
    <div className="kc-content kc-view">
      <h1 className="kc-h1">Admin{section ? ` · ${section}` : ''}</h1>
    </div>
  )
}

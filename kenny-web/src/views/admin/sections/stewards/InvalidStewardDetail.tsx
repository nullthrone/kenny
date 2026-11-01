import { useState } from 'react'
import { Link } from 'react-router'
import type { InvalidSteward } from '../../types'
import { BuilderActions, DeleteBuiltModal, EditEditor } from './BuilderEntry'
import { ModeBadge, shortHash } from './badges'
import shared from '../../shared.module.css'
import styles from './stewards.module.css'

/**
 * A built steward that no longer validates (a release withdrew a tool or a precondition it names).
 * It is kept and shown with its errors and does not run until a person fixes it (ADR-0074 rule 5).
 */
export default function InvalidStewardDetail({ steward, canManage }: { steward: InvalidSteward; canManage: boolean }) {
  const [editing, setEditing] = useState(false)
  const [deleting, setDeleting] = useState(false)

  if (editing) {
    return (
      <EditEditor
        stewardId={steward.id}
        title={steward.title}
        onCancel={() => setEditing(false)}
        onSaved={() => setEditing(false)}
      />
    )
  }

  return (
    <div>
      <Link to="/admin/stewards" className={styles.back}>
        ← ALL STEWARDS
      </Link>
      <div className={styles.detailHead}>
        <h2 className={styles.detailTitle}>{steward.title}</h2>
        <span className={styles.badgeRow}>
          <span className={`${styles.tag} ${styles.toneDanger}`}>INVALID</span>
          <ModeBadge steward={{ mode: steward.mode, act_bound: null }} />
        </span>
      </div>
      {steward.description && (
        <p className={shared.help} style={{ marginBottom: 12 }}>
          {steward.description}
        </p>
      )}
      <div className={shared.errorBox} role="alert">
        <strong>This steward does not run.</strong> What it was saved with no longer validates on this server, so it is kept
        here until a person fixes it. Its standing authorizations are void.
        <ul style={{ margin: '8px 0 0', paddingLeft: 18 }} aria-label="Why it is invalid">
          {steward.errors.map((e, i) => (
            <li key={i}>
              <span className={shared.mono}>{e.field}</span>: {e.error}
            </li>
          ))}
        </ul>
      </div>
      <p className={shared.help} style={{ marginBottom: 16 }}>
        Built in the dashboard{steward.source ? ` (${steward.source})` : ''}
        {steward.version !== undefined ? `, version ${steward.version}` : ''} · hash{' '}
        <span className={styles.hash}>{shortHash(steward.effective_hash)}</span>
      </p>
      {canManage ? (
        <BuilderActions builtin onClone={() => undefined} onEdit={() => setEditing(true)} onDelete={() => setDeleting(true)} />
      ) : (
        <p className={shared.help}>Fixing or deleting a built steward needs a superuser signed in to the dashboard.</p>
      )}
      {deleting && <DeleteBuiltModal steward={steward} onClose={() => setDeleting(false)} />}
    </div>
  )
}

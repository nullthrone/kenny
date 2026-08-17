import { useState } from 'react'
import Modal from '../../components/Modal/Modal'
import { Plus, X, ICON_STROKE_WIDTH } from '../../components/icons'
import styles from './Fleet.module.css'

/**
 * MOUNT POINT, not a real wizard. The 3-step "Add a PC" flow (name → OS →
 * hand-over — download installer or a one-time share link; prototype lines
 * 559-597) is owned by another agent and expected at
 * `src/components/Wizard/`. That path doesn't exist yet in this tree, and a
 * static import of a module that doesn't exist would break the build, so
 * this renders the trigger button plus a minimal placeholder dialog instead.
 *
 * To wire the real wizard once it lands: import it from
 * `src/components/Wizard/` and render `<Wizard onClose={() => setOpen(false)} />`
 * in place of the placeholder body below. The trigger button and open state
 * can stay exactly as they are.
 */
export default function WizardMount() {
  const [open, setOpen] = useState(false)

  return (
    <>
      <button type="button" className={styles.addButton} onClick={() => setOpen(true)}>
        <Plus width={14} height={14} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
        ADD A PC
      </button>
      <Modal open={open} onClose={() => setOpen(false)} labelledBy="add-pc-title" width={520}>
        <div className={styles.wizardHeader}>
          <span id="add-pc-title" className={styles.wizardTitle}>
            ADD A PC
          </span>
          <button type="button" className={styles.wizardClose} onClick={() => setOpen(false)}>
            <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          </button>
        </div>
        <div className={styles.wizardBody}>
          The name → system → hand-over wizard mounts here once it's built. Nothing to
          configure yet — close this and use the installer/share-link routes directly if
          you need to onboard a host now.
        </div>
      </Modal>
    </>
  )
}

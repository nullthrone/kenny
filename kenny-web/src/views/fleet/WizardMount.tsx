import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Wizard from '../../components/Wizard/Wizard'
import { Plus, ICON_STROKE_WIDTH } from '../../components/icons'
import styles from './Fleet.module.css'

/**
 * "ADD A PC" trigger + the 3-step wizard, owned by another agent
 * (`src/components/Wizard/`, landed mid-session — this mounts it rather than
 * the placeholder dialog an earlier version of this file used). Refreshes
 * the fleet grid once a host is actually provisioned (a share link minted,
 * or the installer download started).
 */
export default function WizardMount() {
  const [open, setOpen] = useState(false)
  const queryClient = useQueryClient()

  return (
    <>
      <button type="button" className={styles.addButton} onClick={() => setOpen(true)}>
        <Plus width={14} height={14} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
        ADD A PC
      </button>
      <Wizard
        open={open}
        onClose={() => setOpen(false)}
        onProvisioned={() => queryClient.invalidateQueries({ queryKey: ['fleet'] })}
      />
    </>
  )
}

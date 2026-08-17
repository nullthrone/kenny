import { useEffect, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import styles from './Modal.module.css'

export interface ModalProps {
  open: boolean
  onClose: () => void
  /**
   * SECURITY-RELEVANT, not cosmetic. Default `true`.
   *
   * `false` disables Escape AND backdrop click — there is no way to close
   * the modal except an explicit action inside it. This is the fleet-chat
   * confirmation gate's exact requirement: a state-changing tool call has
   * nothing else to do until the operator decides, and an accidental
   * Escape/outside-click must never be confusable with a decision (let
   * alone with "deny"). The ticket-detail approval gate is the other
   * case — it IS dismissible ("Decide later"), because a ticket may
   * legitimately wait hours for a different operator; the durable
   * Approve/Deny row in the ticket timeline remains the real path back to
   * it. Get this prop right per call site — do not default it away.
   */
  dismissible?: boolean
  /** id of an element inside `children` that labels the dialog, for aria-labelledby. */
  labelledBy?: string
  /** Panel width; the prototype uses 520 (new ticket, wizard) and 680 (section detail). */
  width?: number | string
  children: ReactNode
  className?: string
}

/**
 * Backdrop + centred panel. Panel and backdrop are SIBLINGS, not nested —
 * exactly like the prototype — so a click on the panel never bubbles to
 * the backdrop's click-outside handler and doesn't need stopPropagation.
 *
 * Provides only the outer chrome (backdrop, positioning, animation,
 * Escape/click-outside per `dismissible`). Header content, a close cross,
 * and footer actions are the caller's `children` — GateCard's non-
 * dismissible gate deliberately renders no close cross at all.
 */
export default function Modal({ open, onClose, dismissible = true, labelledBy, width = 520, children, className }: ModalProps) {
  useEffect(() => {
    if (!open || !dismissible) return
    function onKeydown(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeydown)
    return () => window.removeEventListener('keydown', onKeydown)
  }, [open, dismissible, onClose])

  if (!open) return null

  return createPortal(
    <>
      <div className={`${styles.backdrop} kc-backdrop`} onClick={dismissible ? onClose : undefined} />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        className={`${styles.panel} kc-modal${className ? ` ${className}` : ''}`}
        style={{ width }}
      >
        {children}
      </div>
    </>,
    document.body,
  )
}

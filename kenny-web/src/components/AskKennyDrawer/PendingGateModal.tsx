import { useEffect, useRef } from 'react'
import GateCard from '../GateCard/GateCard'
import Modal from '../Modal/Modal'
import type { PendingGate } from '../../chat/types'

export interface PendingGateModalProps {
  gate: PendingGate
  onApprove: () => void
  onDeny: () => void
  busy: boolean
}

/**
 * The fleet chat confirmation gate. SECURITY-CRITICAL (non-negotiable #3):
 * its only exits are CONFIRM & RUN and CANCEL. Everything below exists to
 * make that true in practice, not just on paper:
 *
 * - `Modal dismissible={false}` — per GateCard's own doc comment, this is
 *   exactly the wrapping it expects from "the fleet-chat confirmation".
 *   Escape/click-outside are already no-ops inside Modal for this prop.
 * - No `onDecideLater` is passed to GateCard, so it renders only the two
 *   buttons — there is no third "close without deciding" affordance here.
 * - Modal's own backdrop is a full-viewport `position:fixed` layer painted
 *   above Shell's drawer chrome (same z-index tier, later in paint order),
 *   so it already physically blocks clicks on Shell's header ✕ and its
 *   drawer backdrop — those aren't reachable by pointer while this is open,
 *   with no code in this file needing to know Shell exists.
 * - Escape is the one gap stacking alone doesn't close: Shell registers its
 *   own `chatOpen` Escape handler on `window` at Shell's mount, independent
 *   of this gate. A capture-phase listener added here, only while the gate
 *   is open, intercepts Escape before it reaches that handler — capture
 *   fires top-down before any bubble-phase listener on the same target, so
 *   registration order doesn't matter. This is the one piece of defence
 *   this component adds beyond what Modal already gives every other caller.
 * - A small Tab trap keeps focus cycling between the two buttons, so a
 *   keyboard user tabbing past them can't reach — and activate — whatever
 *   sits behind the (visually blocked) backdrop either.
 */
export default function PendingGateModal({ gate, onApprove, onDeny, busy }: PendingGateModalProps) {
  const panelRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const buttons = () => Array.from(panelRef.current?.querySelectorAll('button') ?? [])
    // Default focus to CANCEL — an accidental Enter should not be the
    // destructive path.
    const initial = buttons()
    initial[initial.length - 1]?.focus()

    function guard(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.stopPropagation()
        e.preventDefault()
        return
      }
      if (e.key === 'Tab') {
        const list = buttons()
        if (list.length === 0) return
        e.preventDefault()
        const idx = list.indexOf(document.activeElement as HTMLButtonElement)
        const nextIdx = e.shiftKey ? (idx <= 0 ? list.length - 1 : idx - 1) : idx === list.length - 1 ? 0 : idx + 1
        list[nextIdx]?.focus()
      }
    }
    window.addEventListener('keydown', guard, true)
    return () => window.removeEventListener('keydown', guard, true)
  }, [])

  return (
    <Modal open onClose={noop} dismissible={false} width={440}>
      <div ref={panelRef} style={{ padding: 4 }}>
        <GateCard
          tool={gate.tool}
          args={gate.args}
          agentId={gate.agentId}
          toolClass={gate.toolClass}
          approveLabel="CONFIRM & RUN"
          denyLabel="CANCEL"
          onApprove={onApprove}
          onDeny={onDeny}
          busy={busy}
        />
      </div>
    </Modal>
  )
}

function noop(): void {
  // Modal requires onClose even when dismissible=false (it's only ever
  // invoked from paths this component doesn't wire up: no backdrop click,
  // no Escape handler, no close cross).
}

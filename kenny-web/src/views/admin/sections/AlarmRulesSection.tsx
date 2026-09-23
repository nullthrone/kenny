import TicketRulesSection from './TicketRulesSection'
import SuppressionsSection from './SuppressionsSection'
import shared from '../shared.module.css'

/**
 * Admin → Alarm rules. The two fleet policies on what an alarm leads to: which
 * alerts open a ticket, and which reliability events never count at all.
 */
export default function AlarmRulesSection() {
  return (
    <div>
      <div className={shared.cardTitle}>AUTO-TICKET RULES</div>
      <TicketRulesSection />
      <div className={shared.cardTitle} style={{ marginTop: 32 }}>
        RELIABILITY SUPPRESSIONS
      </div>
      <SuppressionsSection />
    </div>
  )
}

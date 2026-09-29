import { useState } from 'react'
import AvailabilityBand from '../../components/AvailabilityBand/AvailabilityBand'
import { useAgentAvailability } from './api'

export interface AvailabilityPanelProps {
  agentId: string
}

/** Windows the server answers (`days` is 1..30); 30 is what the page opens on. */
const DAY_OPTIONS = [7, 30] as const

/** The host page's availability card: owns the chosen window and the query,
 * and leaves every pixel to `AvailabilityBand`. */
export default function AvailabilityPanel({ agentId }: AvailabilityPanelProps) {
  const [days, setDays] = useState<number>(30)
  const { data, error, isPlaceholderData } = useAgentAvailability(agentId, days)

  return (
    <AvailabilityBand
      data={data}
      days={days}
      onDaysChange={setDays}
      dayOptions={DAY_OPTIONS}
      stale={isPlaceholderData}
      error={error ? (error instanceof Error ? error.message : 'Unknown error.') : null}
    />
  )
}

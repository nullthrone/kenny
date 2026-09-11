import { fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import LinkedAlerts, { type TicketAlertsResponse } from './LinkedAlerts'

const DATA: TicketAlertsResponse = {
  ticket_id: 't1',
  agent_id: 'oma-pc',
  collected_at: '2026-09-02T08:00:00Z',
  alerts: [
    {
      id: 1,
      at: '2026-09-01T08:00:00Z',
      agent_id: 'oma-pc',
      level: 'crit',
      title: 'oma-pc: disk is nearly full',
      body: 'C: 97% used (>=95%)',
      priority: 'high',
      event_type: 'health',
    },
    {
      id: 2,
      at: '2026-09-02T08:00:00Z',
      agent_id: 'oma-pc',
      level: 'crit',
      title: 'oma-pc: disk is nearly full (again)',
      body: 'C: 98% used (>=95%)',
      priority: 'high',
      event_type: 'health',
    },
  ],
  findings: [
    {
      name: 'disk',
      status: 'crit',
      summary: 'C: nearly full',
      reason: 'C: 98% full (>=95%)',
      since: '2026-09-01T08:00:00Z',
      age_seconds: 86_400,
    },
  ],
}

function renderPanel(data: TicketAlertsResponse) {
  return render(
    <MemoryRouter>
      <LinkedAlerts data={data} />
    </MemoryRouter>,
  )
}

/**
 * The whole point of this panel: reading an alert is part of working the
 * ticket. A click that replaces the screen is the behaviour the queue rework
 * exists to remove, so the panel must contain no link at all — not to the
 * host, not to the section, not to the alert log.
 */
describe('LinkedAlerts never navigates', () => {
  it('expands an alert in place and offers nowhere to go', () => {
    renderPanel(DATA)

    expect(screen.queryByText('C: 97% used (>=95%)')).toBeNull()

    fireEvent.click(screen.getByText('oma-pc: disk is nearly full'))

    expect(screen.getByText('C: 97% used (>=95%)')).toBeInTheDocument()
    expect(screen.queryAllByRole('link')).toHaveLength(0)
  })

  it('expands a finding in place and offers nowhere to go', () => {
    renderPanel(DATA)

    fireEvent.click(screen.getByText('C: 98% full (>=95%)'))

    expect(screen.getByText(/crit since/)).toBeInTheDocument()
    expect(screen.queryAllByRole('link')).toHaveLength(0)
  })
})

describe('LinkedAlerts content', () => {
  it('shows every recurrence, not only the alert that opened the ticket', () => {
    renderPanel(DATA)

    expect(screen.getByText('oma-pc: disk is nearly full')).toBeInTheDocument()
    expect(screen.getByText('oma-pc: disk is nearly full (again)')).toBeInTheDocument()
  })

  it('says a subject is still failing', () => {
    renderPanel(DATA)

    // Scoped to the findings half: the alert rows carry a severity chip too,
    // and "is it still true" is the question only this half answers.
    const current = screen.getByText('Current state').closest('section')!
    expect(within(current).getByText('CRITICAL')).toBeInTheDocument()
    expect(within(current).getByText('C: 98% full (>=95%)')).toBeInTheDocument()
  })

  it('says a subject recovered while the ticket stayed open', () => {
    renderPanel({
      ...DATA,
      findings: [{ ...DATA.findings[0], status: 'ok', reason: 'C: 41% full', age_seconds: 0 }],
    })

    const current = screen.getByText('Current state').closest('section')!
    expect(within(current).getByText('HEALTHY')).toBeInTheDocument()
  })

  it('renders a status the console has no colour for as unknown rather than crashing', () => {
    renderPanel({
      ...DATA,
      findings: [{ ...DATA.findings[0], status: 'degraded' }],
    })

    const current = screen.getByText('Current state').closest('section')!
    expect(within(current).getByText('UNKNOWN')).toBeInTheDocument()
  })

  it('renders nothing at all for a ticket with neither', () => {
    const { container } = renderPanel({ ...DATA, alerts: [], findings: [] })

    expect(container).toBeEmptyDOMElement()
  })

  it('renders the history alone when the findings are gone', () => {
    renderPanel({ ...DATA, findings: [] })

    expect(screen.getByText('Alert history')).toBeInTheDocument()
    expect(screen.queryByText('Current state')).toBeNull()
  })

  it('renders the findings alone when the alerts have aged out of retention', () => {
    renderPanel({ ...DATA, alerts: [] })

    expect(screen.getByText('Current state')).toBeInTheDocument()
    expect(screen.queryByText('Alert history')).toBeNull()
  })
})

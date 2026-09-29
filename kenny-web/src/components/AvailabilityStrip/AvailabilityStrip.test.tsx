import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import AvailabilityStrip from './AvailabilityStrip'

/** 168 hourly cells: mostly online, one mixed hour, one offline, one unknown. */
function week(): (number | null)[] {
  const cells: (number | null)[] = Array(168).fill(1)
  cells[10] = 0.5
  cells[20] = 0
  cells[30] = null
  return cells
}

describe('AvailabilityStrip', () => {
  it('draws one cell per hour of the week, oldest first', () => {
    const { container } = render(<AvailabilityStrip availability={{ online_pct: 97.4, cells: week() }} />)
    const cells = [...container.querySelectorAll('rect[data-cell]')]
    expect(cells).toHaveLength(168)
    expect(parseFloat(cells[0].getAttribute('x')!)).toBe(0)
    expect(parseFloat(cells[84].getAttribute('x')!)).toBeCloseTo(50, 5)
    expect(cells[0].getAttribute('fill')).toBe('var(--ok)')
    expect(cells[10].getAttribute('fill')).toBe('var(--warn)')
    expect(cells[20].getAttribute('fill')).toBe('var(--danger)')
    expect(cells[30].getAttribute('fill')).toBe('var(--border-mid)')
  })

  it('carries its percentage as its name and tooltip', () => {
    render(<AvailabilityStrip availability={{ online_pct: 97.4, cells: week() }} />)
    const strip = screen.getByRole('img', { name: '7-day availability 97.4 %' })
    expect(strip).toHaveAttribute('title', '7-day availability 97.4 %')
  })

  it('renders nothing for a server that does not send the field', () => {
    const { container } = render(<AvailabilityStrip />)
    expect(container).toBeEmptyDOMElement()
  })
})

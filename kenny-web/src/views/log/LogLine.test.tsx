import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import LogLine from './LogLine'

/** jsdom has no layout: fake the line's content width against its box width. */
function stubWidths(scrollWidth: number, clientWidth: number) {
  vi.spyOn(HTMLElement.prototype, 'scrollWidth', 'get').mockReturnValue(scrollWidth)
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(clientWidth)
}

const MESSAGE = 'telemetry insert failed for marianne-pc; keeping tunnel open'

afterEach(() => vi.restoreAllMocks())

describe('LogLine', () => {
  it('renders a line that fits as plain text', () => {
    stubWidths(200, 400)
    render(<LogLine what="kenny.tunnel" message={MESSAGE} />)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('opens the full text in a popover when the line is cut off', () => {
    stubWidths(900, 400)
    render(<LogLine what="kenny.tunnel" message={MESSAGE} />)
    const line = screen.getByRole('button')
    expect(line).toHaveAttribute('aria-expanded', 'false')

    fireEvent.click(line)

    const popover = screen.getByRole('dialog', { name: 'Full log line' })
    expect(popover).toHaveTextContent(`kenny.tunnel ${MESSAGE}`)
    expect(popover).toHaveFocus()
    expect(line).toHaveAttribute('aria-expanded', 'true')
  })

  it('closes on Escape and returns focus to the line', () => {
    stubWidths(900, 400)
    render(<LogLine what="kenny.tunnel" message={MESSAGE} />)
    const line = screen.getByRole('button')
    fireEvent.keyDown(line, { key: 'Enter' })
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    fireEvent.keyDown(document, { key: 'Escape' })

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(line).toHaveFocus()
  })

  it('closes on a click outside but not on one inside the popover', () => {
    stubWidths(900, 400)
    render(<LogLine what="kenny.tunnel" message={MESSAGE} />)
    fireEvent.click(screen.getByRole('button'))

    fireEvent.pointerDown(screen.getByRole('dialog'))
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    fireEvent.pointerDown(document.body)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})

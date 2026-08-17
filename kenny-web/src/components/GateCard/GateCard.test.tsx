import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import GateCard from './GateCard'

describe('GateCard', () => {
  it('renders the tool name and every arg verbatim, in the given key order', () => {
    render(
      <GateCard
        tool="winget_update"
        args={{ id: 'Mozilla.Firefox', silent: true }}
        agentId="mia-desktop"
        onApprove={vi.fn()}
        onDeny={vi.fn()}
      />,
    )

    expect(screen.getByText('winget_update')).toBeInTheDocument()
    // Exact, un-truncated, un-reformatted JSON, keys in the order they were given.
    expect(screen.getByText('{"id":"Mozilla.Firefox","silent":true}')).toBeInTheDocument()
    expect(screen.getByText(/mia-desktop/)).toBeInTheDocument()
  })

  it('renders a value containing HTML-ish characters as literal text, not markup', () => {
    const dangerous = '<img src=x onerror=alert(1)>&nbsp;"quoted"'
    render(
      <GateCard
        tool="powershell_exec"
        args={{ script: dangerous }}
        onApprove={vi.fn()}
        onDeny={vi.fn()}
      />,
    )

    // The literal JSON-escaped string is present as text content …
    const expected = JSON.stringify({ script: dangerous })
    expect(screen.getByText(expected)).toBeInTheDocument()
    // … and it was never parsed as HTML: no <img> element exists.
    expect(document.querySelector('img')).toBeNull()
  })

  it('never truncates a long argument value', () => {
    const long = 'C:\\Users\\oma\\Videos\\'.repeat(50)
    render(<GateCard tool="fs_move" args={{ path: long }} onApprove={vi.fn()} onDeny={vi.fn()} />)
    expect(screen.getByText(JSON.stringify({ path: long }))).toBeInTheDocument()
  })

  it('hides the third action when onDecideLater is not provided', () => {
    render(<GateCard tool="winget_update" args={{}} onApprove={vi.fn()} onDeny={vi.fn()} />)
    expect(screen.queryByText('DECIDE LATER')).not.toBeInTheDocument()
  })

  it('shows the third action, with a custom label, when onDecideLater is provided', () => {
    render(
      <GateCard
        tool="winget_update"
        args={{}}
        onApprove={vi.fn()}
        onDeny={vi.fn()}
        onDecideLater={vi.fn()}
        decideLaterLabel="CANCEL"
      />,
    )
    expect(screen.getByText('CANCEL')).toBeInTheDocument()
  })

  it('calls onApprove/onDeny with the labelled buttons', async () => {
    const onApprove = vi.fn()
    const onDeny = vi.fn()
    render(<GateCard tool="winget_update" args={{}} onApprove={onApprove} onDeny={onDeny} />)
    screen.getByText('APPROVE & RUN').click()
    screen.getByText('DENY').click()
    expect(onApprove).toHaveBeenCalledTimes(1)
    expect(onDeny).toHaveBeenCalledTimes(1)
  })
})

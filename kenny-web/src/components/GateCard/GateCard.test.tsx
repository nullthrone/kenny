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
    // The structured view lists the same args, same order.
    expect(Array.from(document.querySelectorAll('dt')).map((e) => e.textContent)).toEqual(['id', 'silent'])
    expect(screen.getByText('Mozilla.Firefox')).toBeInTheDocument()
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

  it('shows a script as its real lines, line for line, with the exact text recoverable', () => {
    const script = "$ts = Get-Date\nforeach ($s in $x) { Stop-Service -Name $s -Force }\n\n# done"
    render(<GateCard tool="powershell_exec" args={{ script, timeout_s: 180 }} onApprove={vi.fn()} onDeny={vi.fn()} />)

    const code = document.querySelector('code') as HTMLElement
    // Concatenated text is the script itself — no JSON escapes, nothing added, nothing dropped.
    expect(code.textContent).toBe(script)
    expect(code.querySelectorAll('[data-n]')).toHaveLength(4)
    expect(screen.getByText('4 lines · powershell')).toBeInTheDocument()
    // Scalars sit beside it as rows; the exact serialisation is still on the card.
    expect(screen.getByText('timeout_s')).toBeInTheDocument()
    expect(screen.getByText(JSON.stringify({ script, timeout_s: 180 }))).toBeInTheDocument()
  })

  it('does not render markup in a script as elements', () => {
    render(<GateCard tool="shell_exec" args={{ command: '<img src=x onerror=alert(1)>' }} onApprove={vi.fn()} onDeny={vi.fn()} />)
    expect(document.querySelector('img')).toBeNull()
  })

  it('makes an invisible direction-override character visible without altering the text', () => {
    const command = 'echo safe\u202E; rm -rf ~'
    render(<GateCard tool="shell_exec" args={{ command }} onApprove={vi.fn()} onDeny={vi.fn()} />)
    const marker = document.querySelector('[data-cp]') as HTMLElement
    expect(marker.getAttribute('data-cp')).toBe('U+202E')
    expect((document.querySelector('code') as HTMLElement).textContent).toBe(command)
  })

  it('adds no buttons beyond the actions (a Modal traps focus on buttons)', () => {
    render(<GateCard tool="powershell_exec" args={{ script: 'a\nb' }} onApprove={vi.fn()} onDeny={vi.fn()} />)
    expect(Array.from(document.querySelectorAll('button')).map((b) => b.textContent)).toEqual(['APPROVE & RUN', 'DENY'])
  })
})

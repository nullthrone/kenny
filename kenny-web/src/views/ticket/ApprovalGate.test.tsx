import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import ApprovalGate from './ApprovalGate'

/**
 * SECURITY-CRITICAL: a gate's `args` are the operator's only evidence of what
 * approving will actually execute (ADR-0038). `GateCard.test.tsx` pins the
 * renderer; this pins the whole path a real gate takes on the ticket page —
 * `ApprovalGate` → `GateCard` — so a formatting step slipped into the wrapper
 * fails here even though the renderer alone still looks correct.
 *
 * The ticket page is the only surface that offers this decision: the queue
 * shows that a ticket waits for an approval and nothing more (ADR-0059), so
 * this path is where the guarantee has to hold.
 */
describe('ApprovalGate passes args through untouched', () => {
  it('renders a Windows path and a JSON-ish string, with quotes and backslashes, byte-for-byte', () => {
    const dangerousArgs = {
      path: 'C:\\Users\\oma\\Videos\\Family "Backup" <2026>\\',
      note: '{"already":"json-like","script":"<img src=x onerror=alert(1)>"}',
    }

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <ApprovalGate
          approvalId="appr-1"
          tool="fs_move"
          args={dangerousArgs}
          agentId="mia-desktop"
          toolClass="standard_change"
          onDecided={vi.fn()}
        />
      </QueryClientProvider>,
    )

    // Exactly what `GateCard`'s own formatter produces — un-truncated,
    // un-reformatted, keys in server order — must be present as literal text.
    const expected = JSON.stringify(dangerousArgs)
    expect(screen.getByText(expected)).toBeInTheDocument()

    // ...and never parsed as markup: the embedded `<img onerror=...>` must
    // not become a real element, and no literal backslash was stripped.
    expect(document.querySelector('img')).toBeNull()
    expect(expected).toContain('\\\\Users\\\\oma')
    expect(expected).toContain('\\"Backup\\"')
  })
})

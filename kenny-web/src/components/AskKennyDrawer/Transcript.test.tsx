import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import Transcript from './Transcript'
import type { TranscriptItem } from '../../chat/types'
import { KENNY_REPLY } from '../../test/markdownSamples'

describe('Transcript', () => {
  it("renders kenny's reply as markdown", () => {
    const items: TranscriptItem[] = [{ kind: 'assistant', id: 'a1', text: KENNY_REPLY }]
    const { container } = render(<Transcript items={items} />)

    expect(container.querySelectorAll('ul li')).toHaveLength(2)
    expect(container.querySelectorAll('ol li')).toHaveLength(2)
    expect(container.querySelector('strong')).not.toBeNull()
    expect(container.textContent).not.toContain('**')
  })

  it('leaves what the operator typed exactly as they typed it', () => {
    const items: TranscriptItem[] = [{ kind: 'user', id: 'u1', text: KENNY_REPLY }]
    const { container } = render(<Transcript items={items} />)

    expect(container.querySelector('li')).toBeNull()
    expect(container.querySelector('strong')).toBeNull()
    expect(container.textContent).toContain('**Was ich tun kann:**')
  })
})

describe('Transcript — reasoning', () => {
  it('folds reasoning away by default and never prints it as the answer', () => {
    const items: TranscriptItem[] = [
      { kind: 'thinking', id: 't1', text: 'the operator is pushing back' },
      { kind: 'assistant', id: 'a1', text: 'You are right.' },
    ]
    const { container } = render(<Transcript items={items} />)

    const fold = container.querySelector('details')
    expect(fold).not.toBeNull()
    expect(fold!.hasAttribute('open')).toBe(false)
    expect(container.querySelector('summary')!.textContent).toContain('thought about this')
  })

  it('says kenny is still at it only for the block still being written', () => {
    const items: TranscriptItem[] = [
      { kind: 'thinking', id: 't1', text: 'still going' },
    ]
    const { container } = render(<Transcript items={items} openThinkingId="t1" />)

    expect(container.querySelector('summary')!.textContent).toContain('is thinking')
  })
})


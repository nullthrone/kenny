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

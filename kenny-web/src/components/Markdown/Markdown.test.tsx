import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import Markdown from './Markdown'
import { HOSTILE, KENNY_REPLY } from '../../test/markdownSamples'

describe('Markdown', () => {
  it('renders kenny\'s marks as structure, not as punctuation', () => {
    const { container } = render(<Markdown text={KENNY_REPLY} />)

    expect(container.querySelectorAll('ul li')).toHaveLength(2)
    expect(container.querySelectorAll('ol li')).toHaveLength(2)
    expect(container.querySelector('strong')?.textContent).toBe('Was ich tun kann:')
    expect(container.querySelector('code')?.textContent).toBe('diag_eventlog')

    // The point of the whole change: no marker survives as a character.
    expect(container.textContent).not.toContain('**')
    expect(container.textContent).not.toContain('`')
  })

  it('keeps paragraphs apart', () => {
    const { container } = render(<Markdown text={'first\n\nsecond'} />)
    expect(container.querySelectorAll('p')).toHaveLength(2)
  })

  it('does not turn hostile markdown into live markup', () => {
    const { container } = render(<Markdown text={HOSTILE} />)

    // No rehype-raw: HTML in the source stays text, never becomes an element.
    expect(container.querySelector('script')).toBeNull()
    expect(container.innerHTML).toContain('&lt;script&gt;')
    expect(container.textContent).toContain('<img src=x onerror="alert(1)">')

    // No images at all — an LLM-supplied URL must not become an outbound
    // request. The alt text survives so the drop is visible, not silent.
    expect(container.querySelector('img')).toBeNull()
    expect(container.innerHTML).not.toContain('evil.example')
    expect(container.textContent).toContain('tracker')

    const link = container.querySelector('a')
    expect(link?.getAttribute('href') ?? '').not.toContain('javascript:')

    // react-markdown's own mdast handle must not reach the DOM.
    expect(container.innerHTML).not.toContain('node=')
  })

  it('opens links safely when kenny writes one anyway', () => {
    const { container } = render(<Markdown text={'[kenny](https://example.com)'} />)
    const link = container.querySelector('a')
    expect(link?.getAttribute('href')).toBe('https://example.com')
    expect(link?.getAttribute('rel')).toBe('noopener noreferrer')
    expect(link?.getAttribute('target')).toBe('_blank')
  })
})

import type { ReactNode } from 'react'

/**
 * Deliberately minimal, dependency-free markdown for assistant text:
 * paragraphs, **bold**, and `inline code`. No `dangerouslySetInnerHTML`
 * anywhere — every token becomes a React text child, so nothing here can
 * ever be interpreted as markup, gated argument or not.
 *
 * Takes the FULL accumulated buffer on every call (non-negotiable #5) — the
 * caller must not call this per-delta and concatenate JSX; the whole point
 * is that a delta can split a token (`**bo` | `ld**`) and only re-parsing
 * the complete buffer recovers from that.
 */
export function renderMarkdownLite(text: string): ReactNode {
  const paragraphs = text.split(/\n{2,}/)
  return paragraphs.map((para, pIdx) => <p key={pIdx}>{renderInline(para)}</p>)
}

const INLINE_TOKEN = /(\*\*[^*]+\*\*|`[^`]+`|\n)/

function renderInline(text: string): ReactNode[] {
  const parts = text.split(INLINE_TOKEN).filter((p) => p !== '')
  return parts.map((part, i) => {
    if (part === '\n') return <br key={i} />
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      return <strong key={i}>{part.slice(2, -2)}</strong>
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return (
        <code key={i} style={{ fontFamily: 'var(--font-mono)', fontSize: 12 }}>
          {part.slice(1, -1)}
        </code>
      )
    }
    return part
  })
}

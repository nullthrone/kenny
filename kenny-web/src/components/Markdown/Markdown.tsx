import { memo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import styles from './Markdown.module.css'

/**
 * The one renderer for kenny's own prose, on every surface that shows it: the
 * ticket timeline, the ticket's live stream, and the Ask kenny drawer. They
 * render the same text, so they render it the same way.
 *
 * What the subset is matters beyond this file: the two conversational system
 * prompts (`ticket_assistant.py`, `chat.py`) tell the model to stay inside
 * **bold**, `inline code`, and `-`/`1.` lists, and the same reply is also
 * delivered to Discord, which renders exactly that subset natively. Anything
 * wider is still parsed here rather than shown as raw punctuation — a table
 * beats pipe soup — but it is not what the model is asked for.
 *
 * Two rules hold the security line. The server stores and serves this text
 * verbatim (`TicketAssistant.append_message`) — nothing escapes or sanitises
 * it — so React's own text escaping is the only barrier there is:
 *
 * - No `rehype-raw`. Raw HTML in the source is dropped, never parsed, which
 *   keeps `dangerouslySetInnerHTML` out of this codebase entirely.
 * - No images. `img` never renders one: a URL in LLM output must not become an
 *   outbound request from an operator's browser.
 *
 * Links keep react-markdown's default `urlTransform`, which drops
 * `javascript:` and friends; do not widen it.
 *
 * Takes the FULL text on every call. A caller streaming deltas must hold the
 * whole accumulated buffer and pass that — a delta can split a token
 * (`**bo` | `ld**`), and only re-parsing the complete buffer recovers from it.
 */
export interface MarkdownProps {
  text: string
  /** The call site's own type scale; this component sets none of its own. */
  className?: string
}

function Markdown({ text, className }: MarkdownProps) {
  return (
    <div className={className ? `${styles.root} ${className}` : styles.root}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // `node` is react-markdown's own mdast handle, not a DOM attribute —
          // drop it rather than spreading it onto the element.
          a: ({ node: _node, children, ...props }) => (
            <a {...props} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          ),
          // Never an <img>: a URL in LLM output must not become an outbound
          // request from an operator's browser. The alt text still shows, so a
          // dropped image is visible as a gap rather than silently gone.
          img: ({ alt }) => (alt ? <em>{alt}</em> : null),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}

/**
 * Memoised because the ticket stream re-renders it per delta with the whole
 * buffer — up to `_MAX_TRAIL_TEXT_CHARS` of it — and an unchanged string must
 * not cost a re-parse.
 */
export default memo(Markdown)

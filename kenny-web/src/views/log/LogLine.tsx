import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import styles from './LogView.module.css'

/** True while `el`'s content is wider than its box, i.e. the ellipsis is showing. */
function useIsTruncated<T extends HTMLElement>(ref: React.RefObject<T | null>, content: string): boolean {
  const [truncated, setTruncated] = useState(false)
  const measure = useCallback(() => {
    const el = ref.current
    if (el) setTruncated(el.scrollWidth > el.clientWidth)
  }, [ref])

  useLayoutEffect(measure, [measure, content])

  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [ref, measure])

  return truncated
}

/**
 * One log row's subject + message. The row stays single-line; when that cuts
 * the text off, the line becomes clickable and opens the full text in a
 * popover laid over the row itself. Short lines render as plain text.
 */
export default function LogLine({ what, message }: { what: string; message: string }) {
  const lineRef = useRef<HTMLSpanElement>(null)
  const popRef = useRef<HTMLDivElement>(null)
  const truncated = useIsTruncated(lineRef, `${what} ${message}`)
  const [open, setOpen] = useState(false)

  const close = useCallback((restoreFocus: boolean) => {
    setOpen(false)
    if (restoreFocus) lineRef.current?.focus()
  }, [])

  useEffect(() => {
    if (!open) return
    popRef.current?.focus()
    const onPointerDown = (e: PointerEvent) => {
      if (!popRef.current?.contains(e.target as Node)) close(false)
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close(true)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open, close])

  const text = (
    <>
      <span className={styles.what}>{what}</span> <span className={styles.msg}>{message}</span>
    </>
  )

  const expandable = truncated || open
  return (
    <div className={styles.lineCell}>
      <span
        ref={lineRef}
        className={`${styles.line} kc-logline${expandable ? ` ${styles.expandable}` : ''}`}
        {...(expandable && {
          role: 'button',
          tabIndex: 0,
          'aria-haspopup': 'dialog' as const,
          'aria-expanded': open,
          onClick: () => setOpen(true),
          onKeyDown: (e: React.KeyboardEvent) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault()
              setOpen(true)
            }
          },
        })}
      >
        {text}
      </span>
      {open && (
        <div ref={popRef} className={styles.popover} role="dialog" aria-label="Full log line" tabIndex={-1}>
          {text}
        </div>
      )}
    </div>
  )
}

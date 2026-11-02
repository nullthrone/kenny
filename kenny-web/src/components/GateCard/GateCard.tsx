import { Fragment } from 'react'
import { formatArgs, viewArgs, type ArgView } from './formatArgs'
import { splitSuspect, tokenizeLines, type Lang } from './highlight'
import styles from './GateCard.module.css'

export interface GateCardProps {
  tool: string
  /**
   * Frozen at hold time — the resolved target is fixed before the gate
   * runs, so a later host switch can't retarget the call (ADR-0038).
   * Shown in full: never truncated, never edited. The structured view and
   * the RAW JSON beneath it are two readings of this same object.
   */
  args: Record<string, unknown>
  agentId?: string | null
  toolClass?: string
  eyebrow?: string
  approveLabel?: string
  denyLabel?: string
  /** Omit to hide the third button entirely — the fleet-chat gate has only CONFIRM & RUN / CANCEL. */
  onDecideLater?: () => void
  decideLaterLabel?: string
  onApprove: () => void
  onDeny: () => void
  /** Disables all three actions while a decision is in flight. */
  busy?: boolean
  className?: string
}

/**
 * Text with invisible / direction-changing characters made visible. The
 * character stays in the DOM (copy yields the exact text); a label is drawn
 * beside it from `data-cp`, so what runs can't differ from what is read.
 */
function Visible({ text }: { text: string }) {
  return (
    <>
      {splitSuspect(text).map((run, i) =>
        run.suspect ? (
          <span key={i} className={styles.suspect} data-cp={run.suspect} title={`Invisible or direction-changing character ${run.suspect}`}>
            {run.text}
          </span>
        ) : (
          <Fragment key={i}>{run.text}</Fragment>
        ),
      )}
    </>
  )
}

function CodeBlock({ name, text, lang }: { name: string; text: string; lang: Lang }) {
  const lines = tokenizeLines(text, lang)
  return (
    <figure className={styles.code}>
      <figcaption className={styles.codeHead}>
        <span className={styles.codeName}>{name}</span>
        <span>
          {lines.length} {lines.length === 1 ? 'line' : 'lines'}
          {lang !== 'plain' && ` · ${lang}`}
        </span>
      </figcaption>
      <pre className={styles.codeBody} tabIndex={-1}>
        <code>
          {lines.map((tokens, i) => (
            <span key={i} className={styles.line} data-n={i + 1}>
              {tokens.map((tok, j) => (
                <span key={j} className={styles[tok.kind]}>
                  <Visible text={tok.text} />
                </span>
              ))}
              {i < lines.length - 1 ? '\n' : ''}
            </span>
          ))}
        </code>
      </pre>
    </figure>
  )
}

function ArgRow({ arg }: { arg: Exclude<ArgView, { kind: 'code' }> }) {
  return (
    <>
      <dt className={styles.argKey}>{arg.key}</dt>
      <dd className={`${styles.argVal} ${arg.kind === 'literal' ? styles.literal : ''}`}>
        <Visible text={arg.text} />
      </dd>
    </>
  )
}

/**
 * The amber state-changing confirmation card. SECURITY-CRITICAL: this is
 * the operator's only look at what approving will run before it runs.
 *
 * Used both inline (ticket detail, Inbox row) and inside a Modal (the
 * fleet-chat confirmation, which must be wrapped in
 * `<Modal dismissible={false}>` by the caller — GateCard itself has no
 * opinion on dismissibility, that's the surrounding Modal's job).
 *
 * Readability never costs fidelity: code arguments are shown as the exact
 * string value (real line breaks, no re-indenting, no truncation), colour is
 * decided by a lossless tokenizer, invisible characters are made visible, and
 * the exact JSON serialisation stays one click away under RAW JSON. The card
 * adds no button besides the actions: a Modal wrapper traps focus on them.
 */
export default function GateCard({
  tool,
  args,
  agentId,
  toolClass,
  eyebrow = 'STATE-CHANGING · FROZEN ARGUMENTS · NEEDS YOUR DECISION',
  approveLabel = 'APPROVE & RUN',
  denyLabel = 'DENY',
  onDecideLater,
  decideLaterLabel = 'DECIDE LATER',
  onApprove,
  onDeny,
  busy = false,
  className,
}: GateCardProps) {
  const metaParts = [toolClass, agentId].filter((p): p is string => Boolean(p))
  const views = viewArgs(tool, args)
  const rows = views.filter((v): v is Exclude<ArgView, { kind: 'code' }> => v.kind !== 'code')
  const blocks = views.filter((v): v is Extract<ArgView, { kind: 'code' }> => v.kind === 'code')

  return (
    <div className={`${styles.card}${className ? ` ${className}` : ''}`}>
      <div className={`${styles.eyebrow} kc-caps`}>{eyebrow}</div>
      <div className={styles.call}>
        <span className={styles.tool}>{tool}</span>
        {metaParts.length > 0 && <span className={styles.meta}> · {metaParts.join(' · ')}</span>}
      </div>
      {rows.length > 0 && (
        <dl className={styles.argList}>
          {rows.map((arg) => (
            <ArgRow key={arg.key} arg={arg} />
          ))}
        </dl>
      )}
      {blocks.map((b) => (
        <CodeBlock key={b.key} name={b.key} text={b.text} lang={b.lang} />
      ))}
      <details className={styles.raw}>
        <summary className={styles.rawSummary}>RAW JSON</summary>
        <pre className={styles.rawBody}>{formatArgs(args)}</pre>
      </details>
      <div className={`${styles.actions} kc-actions`}>
        <button type="button" className={styles.approve} onClick={onApprove} disabled={busy}>
          {approveLabel}
        </button>
        <button type="button" className={styles.deny} onClick={onDeny} disabled={busy}>
          {denyLabel}
        </button>
        {onDecideLater && (
          <button type="button" className={styles.decideLater} onClick={onDecideLater} disabled={busy}>
            {decideLaterLabel}
          </button>
        )}
      </div>
    </div>
  )
}

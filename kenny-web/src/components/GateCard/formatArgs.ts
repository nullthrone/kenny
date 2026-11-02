import type { Lang } from './highlight'

/**
 * Renders a gate's frozen arguments deterministically for display.
 *
 * These args are the operator's only evidence of what approving will
 * actually execute (ADR-0038, types.ts's InboxGate doc comment) — this
 * function must never truncate, reformat differently on re-render, or
 * re-order keys. `JSON.stringify` on an object already parsed from the
 * server's own JSON preserves that object's original key insertion order,
 * so this is the same key order the server sent, not an alphabetised or
 * otherwise "cleaned up" one.
 *
 * This exact serialisation stays on the card (RAW JSON); the structured view
 * below is a reading aid built from the same object, never a replacement.
 */
export function formatArgs(args: Record<string, unknown>): string {
  return JSON.stringify(args)
}

/** Argument names that carry code for the shell tools (`{script}` / `{command}`). */
const CODE_KEYS = new Set(['script', 'command'])

/** A string value this long reads better as a block than inline, even without a newline. */
const BLOCK_MIN_LENGTH = 120

export type ArgView =
  | { kind: 'code'; key: string; text: string; lang: Lang }
  | { kind: 'text'; key: string; text: string }
  | { kind: 'literal'; key: string; text: string }

export function codeLang(tool: string): Lang {
  if (tool === 'powershell_exec') return 'powershell'
  if (tool === 'shell_exec') return 'shell'
  return 'plain'
}

/**
 * One entry per top-level argument, in the server's key order. A string that
 * carries code, spans lines, or is long becomes a `code` block holding the
 * string VALUE — what the interpreter receives, with its real line breaks —
 * rather than the JSON-escaped form with literal `\n`. Everything else is a
 * `text` (string) or `literal` (number, boolean, null, nested JSON) row.
 */
export function viewArgs(tool: string, args: Record<string, unknown>): ArgView[] {
  return Object.entries(args).map(([key, value]): ArgView => {
    if (typeof value === 'string') {
      const isCode = CODE_KEYS.has(key) || value.includes('\n') || value.length > BLOCK_MIN_LENGTH
      return isCode ? { kind: 'code', key, text: value, lang: codeLang(tool) } : { kind: 'text', key, text: value }
    }
    return { kind: 'literal', key, text: JSON.stringify(value) ?? String(value) }
  })
}

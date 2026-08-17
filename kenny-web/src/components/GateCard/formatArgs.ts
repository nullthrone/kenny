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
 */
export function formatArgs(args: Record<string, unknown>): string {
  return JSON.stringify(args)
}

/**
 * The two other localStorage keys the old dashboard used, preserved
 * verbatim for the views that will need them (chat composer, Ask Kenny
 * rail). Renaming either key silently discards every returning operator's
 * saved preference (notes/api-contract-actual.md §4) — do not rename.
 *
 * Not wired to any UI yet; these are read/write primitives for the next
 * wave's chat/composer views to build on.
 */
import { safeGetItem, safeSetItem } from './theme/storage'

const ENTER_TO_SEND_KEY = 'kenny-enter-send'
const COPILOT_KEY = 'kenny-copilot'

/** `"on"` | `"off"`. Default is OFF — Enter inserts a newline unless opted in. */
export function readEnterToSend(): boolean {
  return safeGetItem(ENTER_TO_SEND_KEY) === 'on'
}

export function writeEnterToSend(on: boolean): void {
  safeSetItem(ENTER_TO_SEND_KEY, on ? 'on' : 'off')
}

/** `"on"` | `"off"`. Default is ON — absence (not `"off"`) means the rail is open. */
export function readCopilotOpen(): boolean {
  return safeGetItem(COPILOT_KEY) !== 'off'
}

export function writeCopilotOpen(on: boolean): void {
  safeSetItem(COPILOT_KEY, on ? 'on' : 'off')
}

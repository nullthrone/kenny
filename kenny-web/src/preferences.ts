/**
 * Operator preferences held in localStorage.
 *
 * The key name is the compatibility surface: renaming one silently discards
 * every returning operator's saved preference, so `kenny-enter-send` keeps its
 * name and its `"on"`/`"off"` value format from the previous dashboard.
 *
 * The old dashboard also stored `kenny-copilot` — whether the docked Ask kenny
 * rail was open. That preference does not survive the redesign, because the
 * thing it described no longer exists: Ask kenny is an overlay drawer summoned
 * with ⌘K, and an overlay that reopens itself on load would cover the view the
 * operator navigated to. The key is left in place, unread and unwritten, rather
 * than repurposed for a setting it never meant.
 */
import { safeGetItem, safeSetItem } from './theme/storage'

const ENTER_TO_SEND_KEY = 'kenny-enter-send'

/** `"on"` | `"off"`. Default is OFF — Enter inserts a newline unless opted in. */
export function readEnterToSend(): boolean {
  return safeGetItem(ENTER_TO_SEND_KEY) === 'on'
}

export function writeEnterToSend(on: boolean): void {
  safeSetItem(ENTER_TO_SEND_KEY, on ? 'on' : 'off')
}

/** What a keypress in a composer should do. */
export type ComposerKeyAction = 'send' | 'newline'

/**
 * The one place that decides whether an Enter press sends.
 *
 * Both composers — the Ask kenny drawer's and the ticket's — call this, because
 * they honour a single preference and previously disagreed about it: the drawer
 * read the setting while the ticket composer always sent on Enter, so the same
 * key did different things two clicks apart.
 *
 * With the preference OFF (the default) Enter inserts a newline and Cmd/Ctrl+Enter
 * sends; with it ON, Enter sends and Shift+Enter inserts a newline. Shift+Enter is
 * a newline either way, so the muscle memory that works in one mode still works in
 * the other.
 */
export function composerKeyAction(
  e: { key: string; shiftKey: boolean; metaKey: boolean; ctrlKey: boolean },
  enterSends: boolean = readEnterToSend(),
): ComposerKeyAction {
  if (e.key !== 'Enter') return 'newline'
  if (e.shiftKey) return 'newline'
  if (enterSends) return 'send'
  return e.metaKey || e.ctrlKey ? 'send' : 'newline'
}

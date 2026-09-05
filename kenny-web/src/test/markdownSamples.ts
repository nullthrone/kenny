/**
 * The markdown every kenny dialog surface must render the same way.
 *
 * Three surfaces show kenny's prose — the ticket timeline, the ticket's live
 * stream, and the Ask kenny drawer — and they once disagreed: the drawer
 * parsed markdown while the timeline printed the asterisks. Each surface's
 * test renders THIS text, so a surface that stops rendering markdown fails
 * while the others still pass, which is the divergence itself.
 *
 * The About dialog's changelog is a fourth consumer of the same renderer. Its
 * text is not kenny's — it is release notes written on GitHub — so it shares
 * these samples for the renderer's behaviour, and `HOSTILE` matters more there
 * than anywhere: that body is third-party text.
 */

/** A reply in the shape kenny actually writes: lead-in, bold, bullets, a numbered list. */
export const KENNY_REPLY = [
  'Schau, das Ticket besagt: auf Linus PC treten DCOM-Fehler auf.',
  '',
  '**Was ich tun kann:**',
  '',
  '- Die Event-Logs durchsuchen',
  '- Prüfen, welche Fehler wiederholt vorkommen',
  '',
  'Sobald der PC erreichbar ist:',
  '',
  '1. **Event-Logs sammeln** über `diag_eventlog`',
  '2. Eine Unterdrückungsregel vorschlagen',
].join('\n')

/**
 * Markdown that must NOT become live markup. The server stores and serves
 * kenny's text verbatim, so this is the barrier, not a nicety.
 */
export const HOSTILE = [
  '<img src=x onerror="alert(1)">',
  '',
  '<script>alert(2)</script>',
  '',
  '[click me](javascript:alert(3))',
  '',
  '![tracker](http://evil.example/px.gif)',
].join('\n')

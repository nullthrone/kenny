import { readdirSync, readFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * Structural guard: focusing a control never zooms the page.
 *
 * WebKit on iOS zooms the whole page in when a text-entry control is focused
 * whose computed font-size is below 16px, and does not zoom back out. Every
 * control this app draws is below that line — the form controls sit at
 * `--text-sm` (13px) or smaller, and even the body default `--text-md` is
 * 15px — so the floor is held by one rule in `global.css` rather than by 20
 * module stylesheets agreeing with each other.
 *
 * What that makes checkable here is NOT "no module declares below 16px": the
 * global rule is `!important`, so it already overrides every one of them, and
 * asserting otherwise would only fight the deliberate desktop design. The
 * seam is that the global rule exists and still WINS, which has exactly two
 * halves — the rule is present and correctly gated, and nothing else raises
 * `font-size` with an `!important` of its own, that being the one way to
 * defeat it (an `!important` author declaration also outranks an inline
 * `style`, so there is no third).
 *
 * jsdom has no layout engine and CSS Modules reach a test only as a
 * class-name proxy, so no rendering test here can see a computed font-size —
 * the stylesheets themselves are what gets checked. The other half, the one
 * only a browser can answer (what each control actually computes to, with
 * inheritance and specificity resolved), is
 * `scripts/screenshots/zoom_audit.py`.
 */

/** Read from disk, not through the module graph: vitest hands a `.module.css`
 * import its class-name proxy and ignores `?raw`, so a glob would parse an
 * empty object and pass vacuously. */
const STYLE_ROOT = join(process.cwd(), 'src')
const GLOBAL_CSS = 'global.css'

function cssFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) return cssFiles(path)
    return entry.name.endsWith('.css') ? [path] : []
  })
}

const STYLESHEETS: Record<string, string> = Object.fromEntries(
  cssFiles(STYLE_ROOT).map((file) => [relative(STYLE_ROOT, file), readFileSync(file, 'utf8')]),
)

const GLOBAL = STYLESHEETS[join('styles', GLOBAL_CSS)]

/** The `@media` block that holds the floor, prelude through closing brace. */
const NO_ZOOM_BLOCK = /@media\s*\(hover:\s*none\)\s*and\s*\(pointer:\s*coarse\)\s*\{([\s\S]*?)\n\}/

const FONT_SIZE_IMPORTANT = /font-size\s*:[^;]*!important/
const EXEMPT = /no-zoom-exempt:\s*\S/

/** Innermost `prelude { body }` pairs — same reader as containment.test.ts.
 * `[^{}]` cannot span a nested brace, so every match is a real declaration
 * block, and the prelude carries the comment written above the rule, which is
 * where an exemption is documented. */
const RULE = /([^{}]*)\{([^{}]*)\}/g

interface Rule {
  file: string
  selector: string
  /** Rule text as authored, comments included — where an exemption is written. */
  source: string
  /** Declarations only, so prose that merely names a property is not read as
   * declaring it. */
  declarations: string
}

function uncomment(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, '')
}

const RULES: Rule[] = Object.entries(STYLESHEETS).flatMap(([file, source]) =>
  [...source.matchAll(RULE)].map(([, prelude, body]) => ({
    file,
    selector: uncomment(prelude).trim().replace(/\s+/g, ' '),
    source: prelude + body,
    declarations: uncomment(body),
  })),
)

describe('no zoom on focus', () => {
  it('reads the app stylesheets', () => {
    // A glob or parser that silently matched nothing would pass every
    // assertion below without checking a single declaration.
    expect(Object.keys(STYLESHEETS).length).toBeGreaterThan(30)
    expect(RULES.length).toBeGreaterThan(200)
    expect(GLOBAL).toBeTypeOf('string')
  })

  it('holds a 16px floor for the controls WebKit zooms for', () => {
    const block = GLOBAL.match(NO_ZOOM_BLOCK)
    // Gated on the pointer, not the viewport: an iPhone in landscape is wider
    // than the 760px breakpoint and still zooms.
    expect(block, 'global.css has no (hover: none) and (pointer: coarse) block').not.toBeNull()

    const body = uncomment(block![1])
    expect(body).toMatch(/font-size\s*:\s*16px\s*!important/)
    // 16px is WebKit's threshold, not a step on the type scale — a token here
    // would let the scale move it.
    expect(body).not.toMatch(/font-size\s*:\s*var\(/)

    // Text entry of every kind, and <select>. An `input` with no type
    // attribute is a text field, so the selector subtracts the types that do
    // not zoom rather than listing the ones that do.
    expect(body).toMatch(/(^|,)\s*textarea\s*[,{]/)
    expect(body).toMatch(/(^|,)\s*select\s*[,{]/)
    expect(body).toMatch(/(^|,)\s*input:not\(/)
    for (const type of ['checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'color', 'file'])
      expect(body, `input[type=${type}] is not excluded`).toMatch(
        new RegExp(`:not\\(\\[type=['"]${type}['"]\\]\\)`),
      )
  })

  it('leaves no stylesheet able to outrank the floor', () => {
    // `!important` is the only way past the rule above, so it stays global.css's
    // alone. A rule that genuinely needs it opts out with a
    // `no-zoom-exempt: <reason>` comment, which makes the exception reviewable
    // instead of silent.
    const offenders = RULES.filter((r) => !r.file.endsWith(GLOBAL_CSS))
      .filter((r) => FONT_SIZE_IMPORTANT.test(r.declarations))
      .filter((r) => !EXEMPT.test(r.source))
      .map((r) => `${r.file} — ${r.selector}`)

    expect(offenders).toEqual([])
  })
})

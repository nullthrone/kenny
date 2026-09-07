import { readdirSync, readFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * Structural guard: an inner box never crosses the border of the box around it.
 *
 * The overflow this app produces always has the same shape — a child that
 * cannot wrap (`white-space: nowrap`) and cannot shrink (`flex-shrink: 0`, or
 * a flex item's default `min-width: auto`) is laid out at its max-content
 * width. When the string is server-supplied (a health `reason`, a failing-KB
 * list, a path, a hostname) that width is unbounded, so the child paints past
 * its card's border instead of the card growing to fit it.
 *
 * `white-space: nowrap` is therefore only allowed together with BOTH halves of
 * its containment: a width bound (`max-width`), so the box stays inside its
 * parent, and a clip (`overflow: clip|hidden`), so the text stays inside the
 * box. Either half alone still overflows — bounding leaves the text running
 * out past the border, clipping leaves the box free to widen the row.
 *
 * A rule that genuinely needs an unbounded nowrap (a cell inside a horizontal
 * scroller, the visually-hidden pattern) opts out with a
 * `containment-exempt: <reason>` comment, which makes the exception
 * reviewable instead of silent.
 *
 * jsdom has no layout engine, so no rendering test here can catch this class of
 * bug — the stylesheets themselves are what gets checked. The other half, the
 * one only a browser can see (siblings that overflow only together, a fixed
 * min-width under a narrow parent), is `scripts/screenshots/overflow_audit.py`.
 */

/** Read from disk, not through the module graph: vitest hands a `.module.css`
 * import its class-name proxy and ignores `?raw`, so a glob would parse an
 * empty object and pass vacuously. */
const STYLE_ROOT = join(process.cwd(), 'src')

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

const NOWRAP = /white-space\s*:\s*nowrap/
const MAX_WIDTH = /max-width\s*:/
const CLIP = /overflow(-x)?\s*:\s*(clip|hidden)/
const EXEMPT = /containment-exempt:\s*\S/

/** Innermost `prelude { body }` pairs. `[^{}]` cannot span a nested brace, so
 * at-rule wrappers (`@media`) fall into the following rule's prelude and every
 * match is a real declaration block. The prelude carries the comment written
 * above the rule, which is where an exemption is documented. */
const RULE = /([^{}]*)\{([^{}]*)\}/g

interface Rule {
  file: string
  selector: string
  /** Rule text as authored, comments included — where an exemption is written. */
  source: string
  /** Declarations only. Comments are stripped so prose that merely NAMES a
   * property (Shell's `.versionLine` explains why it is *not* nowrap) is not
   * read as declaring it. */
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

function ruleFor(selector: string): Rule {
  const found = RULES.find((r) => r.selector === selector)
  if (!found) throw new Error(`no rule for ${selector}`)
  return found
}

describe('nowrap containment', () => {
  it('reads the app stylesheets', () => {
    // A glob or parser that silently matched nothing would pass every
    // assertion below without checking a single declaration.
    expect(Object.keys(STYLESHEETS).length).toBeGreaterThan(30)
    expect(RULES.length).toBeGreaterThan(200)
  })

  it('bounds and clips every white-space: nowrap rule', () => {
    const offenders = RULES.filter((r) => NOWRAP.test(r.declarations))
      .filter((r) => !EXEMPT.test(r.source))
      .filter((r) => !(MAX_WIDTH.test(r.declarations) && CLIP.test(r.declarations)))
      .map((r) => `${r.file} — ${r.selector}`)

    expect(offenders).toEqual([])
  })

  it('makes every exemption state its reason', () => {
    const exempt = RULES.filter((r) => /containment-exempt/.test(r.source))
    expect(exempt.length).toBeGreaterThan(0)
    expect(exempt.filter((r) => !EXEMPT.test(r.source)).map((r) => r.selector)).toEqual([])
  })
})

describe('containment primitives', () => {
  it('kc-box clips at the border', () => {
    // `clip`, not `hidden`: `hidden` would make every card a scroll container.
    expect(ruleFor('.kc-box').declarations).toMatch(/overflow\s*:\s*clip/)
  })

  it('kc-evidence wraps server text instead of widening its row', () => {
    const { declarations } = ruleFor('.kc-evidence')
    // The cap is what makes the wrapping bite: `overflow-wrap` does not lower
    // an element's min-content width, so without it a flex item is still laid
    // out at max-content and escapes its parent however freely it may wrap.
    expect(declarations).toMatch(/max-width\s*:\s*100%/)
    expect(declarations).toMatch(/white-space\s*:\s*normal/)
    expect(declarations).toMatch(/overflow-wrap\s*:\s*break-word/)
  })

  it('keeps the health rule chip on the wrapping treatment', () => {
    // The chip that started this: SectionList's `.rule` renders the health
    // rule's `reason` verbatim, and reliability's reason is a full sentence.
    const { declarations } = ruleFor('.rule')
    expect(declarations).toMatch(/composes\s*:\s*kc-evidence from global/)
    expect(declarations).not.toMatch(NOWRAP)
  })
})

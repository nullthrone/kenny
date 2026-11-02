/**
 * Lossless syntax tokenizer for the code a gate is about to run.
 *
 * DISPLAY ONLY, and lossless by construction: `tokens.map(t => t.text).join('')`
 * is always exactly the input. Nothing is dropped, normalised or re-indented —
 * the highlighter decides colour, never content (the gate's arguments are the
 * operator's only evidence of what approving will run, ADR-0038). A wrong
 * guess here costs a wrong colour, never a different script on screen.
 *
 * Not a parser. It recognises just enough of PowerShell and POSIX shell to
 * colour the things an operator reads for: strings, variables, cmdlets,
 * parameters, comments. Code that executes inside a double-quoted string —
 * `"$(Remove-Item …)"` — is tokenised as code, not swallowed into the string,
 * so a command cannot hide behind string colouring.
 */

export type Lang = 'powershell' | 'shell' | 'plain'

export type TokenKind =
  | 'plain'
  | 'comment'
  | 'string'
  | 'variable'
  | 'cmdlet'
  | 'keyword'
  | 'param'
  | 'number'
  | 'op'

export interface Token {
  kind: TokenKind
  text: string
}

const PS_KEYWORDS = new Set(
  (
    'if elseif else foreach for while do switch function filter param return try catch finally throw ' +
    'break continue in begin process end trap until class using data exit'
  ).split(' '),
)

const SH_KEYWORDS = new Set(
  (
    'if then elif else fi for while until do done case esac in function select time return exit ' +
    'break continue'
  ).split(' '),
)

/** Operators PowerShell spells like parameters (`-eq`, `-and`, …). */
const PS_WORD_OPS = new Set(
  (
    'eq ne gt ge lt le ceq cne cgt cge clt cle ieq ine like notlike match notmatch contains notcontains ' +
    'in notin is isnot as and or not xor band bor bxor bnot shl shr replace split join f'
  ).split(' '),
)

/** PowerShell approved verbs — a hyphenated word is only a cmdlet if it starts with one. */
const PS_VERBS = new Set(
  (
    'add approve assert backup block checkpoint clear close compare complete compress confirm connect ' +
    'convert convertfrom convertto copy debug deny disable disconnect dismount edit enable enter exit ' +
    'expand export find foreach format get grant group hide import initialize install invoke join limit ' +
    'lock measure merge mount move new open optimize out ping pop protect publish push read receive redo ' +
    'register remove rename repair request reset resize resolve restart restore resume revoke save search ' +
    'select send set show skip sort split start step stop submit suspend switch sync test trace unblock ' +
    'undo uninstall unlock unprotect unpublish unregister update use wait watch where write'
  ).split(' '),
)

const RE = {
  ws: /\s+/y,
  hereString: /@(["'])[ \t]*\r?\n[\s\S]*?\r?\n\1@/y,
  blockComment: /<#[\s\S]*?(?:#>|$)/y,
  lineComment: /#[^\n]*/y,
  psDouble: /"(?:`[\s\S]|""|[^"`])*"?/y,
  psSingle: /'(?:''|[^'])*'?/y,
  shDouble: /"(?:\\[\s\S]|[^"\\])*"?/y,
  shSingle: /'[^']*'?/y,
  variable: /\$(?:\{[^}\n]*\}?|[A-Za-z_?^$][\w:]*|\d+|[@*#!-])/y,
  splat: /@[A-Za-z_]\w*/y,
  number: /\d+(?:\.\d+)?(?![\w.])/y,
  word: /[A-Za-z_][\w]*(?:-[A-Za-z_][\w]*)*/y,
  psParam: /-[A-Za-z][\w-]*:?/y,
  shParam: /--?[A-Za-z][\w-]*/y,
  op: /[{}()[\];,|&<>=+*/%!.:@\\]+/y,
}

function matchAt(re: RegExp, src: string, i: number): string | null {
  re.lastIndex = i
  const m = re.exec(src)
  return m && m[0].length > 0 ? m[0] : null
}

/** True when `i` starts a new word: start of input or after whitespace / a separator. */
function atBoundary(src: string, i: number): boolean {
  return i === 0 || /[\s;|&({}\])`=,]/.test(src[i - 1])
}

/**
 * `$(` starts a subexpression inside a double-quoted string; returns the
 * index just past its balanced `)` (or the end of the string if unbalanced).
 */
function subexprEnd(s: string, open: number): number {
  let depth = 0
  for (let i = open; i < s.length; i++) {
    if (s[i] === '(') depth++
    else if (s[i] === ')' && --depth === 0) return i + 1
  }
  return s.length
}

/** Split a double-quoted string into string text, variables and executable `$(…)` code. */
function interpolate(str: string, lang: Lang, out: Token[]): void {
  let plainStart = 0
  const flush = (end: number) => {
    if (end > plainStart) out.push({ kind: 'string', text: str.slice(plainStart, end) })
  }
  let i = 0
  while (i < str.length) {
    const c = str[i]
    // PowerShell escapes with a backtick, shell with a backslash: the next char is literal.
    if ((lang === 'powershell' && c === '`') || (lang === 'shell' && c === '\\')) {
      i += 2
      continue
    }
    if (c === '$' && str[i + 1] === '(') {
      flush(i)
      const end = Math.min(subexprEnd(str, i + 1), str.length)
      const closed = str[end - 1] === ')'
      const innerEnd = closed ? end - 1 : end
      out.push({ kind: 'op', text: '$(' })
      tokenizeInto(str.slice(i + 2, innerEnd), lang, out)
      if (closed) out.push({ kind: 'op', text: ')' })
      i = end
      plainStart = i
      continue
    }
    if (c === '$') {
      const v = matchAt(RE.variable, str, i)
      if (v) {
        flush(i)
        out.push({ kind: 'variable', text: v })
        i += v.length
        plainStart = i
        continue
      }
    }
    i++
  }
  flush(str.length)
}

function tokenizeInto(src: string, lang: Lang, out: Token[]): void {
  const ps = lang === 'powershell'
  const keywords = ps ? PS_KEYWORDS : SH_KEYWORDS
  let i = 0
  const push = (kind: TokenKind, text: string) => {
    out.push({ kind, text })
    i += text.length
  }

  while (i < src.length) {
    let t: string | null

    if ((t = matchAt(RE.ws, src, i))) {
      push('plain', t)
      continue
    }
    if (ps && (t = matchAt(RE.hereString, src, i))) {
      push('string', t)
      continue
    }
    if (ps && (t = matchAt(RE.blockComment, src, i))) {
      push('comment', t)
      continue
    }
    if (src[i] === '#' && atBoundary(src, i) && (t = matchAt(RE.lineComment, src, i))) {
      push('comment', t)
      continue
    }
    if (src[i] === '"' && (t = matchAt(ps ? RE.psDouble : RE.shDouble, src, i))) {
      interpolate(t, lang, out)
      i += t.length
      continue
    }
    if (src[i] === "'" && (t = matchAt(ps ? RE.psSingle : RE.shSingle, src, i))) {
      push('string', t)
      continue
    }
    if (src[i] === '$' && (t = matchAt(RE.variable, src, i))) {
      push('variable', t)
      continue
    }
    if (ps && src[i] === '@' && (t = matchAt(RE.splat, src, i))) {
      push('variable', t)
      continue
    }
    if (src[i] === '-' && atBoundary(src, i) && (t = matchAt(ps ? RE.psParam : RE.shParam, src, i))) {
      const name = t.slice(1).replace(/:$/, '').toLowerCase()
      push(ps && PS_WORD_OPS.has(name) ? 'op' : 'param', t)
      continue
    }
    if (/\d/.test(src[i]) && !/[\w.-]/.test(src[i - 1] ?? ' ') && (t = matchAt(RE.number, src, i))) {
      push('number', t)
      continue
    }
    if ((t = matchAt(RE.word, src, i))) {
      const prev = src[i - 1] ?? ' '
      const lower = t.toLowerCase()
      // A word glued to a path or member access (`C:\a\foo-bar`, `$x.Name`) is data, not a command.
      const glued = /[\\/.:$]/.test(prev)
      let kind: TokenKind = 'plain'
      if (!glued) {
        if (ps && t.includes('-') && PS_VERBS.has(lower.split('-')[0])) kind = 'cmdlet'
        else if (keywords.has(lower)) kind = 'keyword'
      }
      push(kind, t)
      continue
    }
    if ((t = matchAt(RE.op, src, i))) {
      push('op', t)
      continue
    }
    push('plain', src[i])
  }
}

/** Merge neighbouring tokens of one kind so the DOM carries few spans. */
function coalesce(tokens: Token[]): Token[] {
  const merged: Token[] = []
  for (const t of tokens) {
    const last = merged[merged.length - 1]
    if (last && last.kind === t.kind && t.kind !== 'cmdlet') last.text += t.text
    else merged.push({ ...t })
  }
  return merged
}

export function tokenize(src: string, lang: Lang): Token[] {
  if (lang === 'plain') return src ? [{ kind: 'plain', text: src }] : []
  const out: Token[] = []
  tokenizeInto(src, lang, out)
  return coalesce(out)
}

/** Lines of tokens. A token that spans a newline is split, so each line renders on its own. */
export function tokenizeLines(src: string, lang: Lang): Token[][] {
  const lines: Token[][] = [[]]
  for (const tok of tokenize(src.replace(/\r\n/g, '\n'), lang)) {
    const parts = tok.text.split('\n')
    parts.forEach((part, idx) => {
      if (idx > 0) lines.push([])
      if (part) lines[lines.length - 1].push({ kind: tok.kind, text: part })
    })
  }
  return lines
}

/**
 * Characters that are invisible or reorder text, so what the operator reads
 * can differ from what runs: C0/C1 controls, soft hyphen, zero-width and
 * bidi-control characters (Trojan Source), BOM. Tab and newline are ordinary.
 */
const SUSPECT =
  // eslint-disable-next-line no-control-regex
  /[\u0000-\u0008\u000B-\u001F\u007F-\u009F\u00AD\u061C\u180E\u200B-\u200F\u2028\u2029\u202A-\u202E\u2060-\u206F\uFEFF\uFFF9-\uFFFB]/g

export interface TextRun {
  text: string
  /** Set for a suspect character: a label such as `U+202E`. The character itself stays in `text`. */
  suspect?: string
}

export function splitSuspect(text: string): TextRun[] {
  const runs: TextRun[] = []
  let last = 0
  for (const m of text.matchAll(SUSPECT)) {
    const at = m.index ?? 0
    if (at > last) runs.push({ text: text.slice(last, at) })
    const cp = m[0].codePointAt(0) ?? 0
    runs.push({ text: m[0], suspect: `U+${cp.toString(16).toUpperCase().padStart(4, '0')}` })
    last = at + m[0].length
  }
  if (last < text.length) runs.push({ text: text.slice(last) })
  return runs
}

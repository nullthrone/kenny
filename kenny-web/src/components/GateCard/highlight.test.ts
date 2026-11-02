import { describe, expect, it } from 'vitest'
import { splitSuspect, tokenize, tokenizeLines } from './highlight'

const SCRIPT = [
  "$ErrorActionPreference = 'Continue'",
  "$ts = Get-Date -Format 'yyyyMMddHHmmss'",
  "foreach ($s in 'wuauserv','bits') { Stop-Service -Name $s -Force -ErrorAction SilentlyContinue }",
  'Start-Sleep -Seconds 3',
  'if (Test-Path "$env:windir\\SoftwareDistribution") { Rename-Item -Path "$env:windir\\SoftwareDistribution" -NewName "SD.bak-$ts" }',
  "# restart\nGet-Service -Name $svcs | Format-Table -AutoSize",
].join('\n')

const SAMPLES: Array<[string, 'powershell' | 'shell']> = [
  [SCRIPT, 'powershell'],
  ['<# block\ncomment #>\nWrite-Host @"\nhere $x\n"@', 'powershell'],
  ['echo "a $(rm -rf /tmp/x) b" # trailing\nfor f in *.log; do gzip "$f"; done', 'shell'],
  ['"unterminated $(Remove-Item', 'powershell'],
  ["'it''s' `\n -and $true -eq 3.5", 'powershell'],
  ['', 'powershell'],
  ['\u202Eevil\u200B', 'shell'],
]

describe('tokenize', () => {
  it.each(SAMPLES)('is lossless: tokens concatenate back to the exact input (%#)', (src, lang) => {
    expect(tokenize(src, lang).map((t) => t.text).join('')).toBe(src)
  })

  it('is lossless for lines too, modulo CRLF normalisation', () => {
    const lines = tokenizeLines(SCRIPT.replace(/\n/g, '\r\n'), 'powershell')
    const rebuilt = lines.map((l) => l.map((t) => t.text).join('')).join('\n')
    expect(rebuilt).toBe(SCRIPT)
  })

  it('classifies the things an operator reads for', () => {
    const kinds = (src: string) => tokenize(src, 'powershell').filter((t) => t.kind !== 'plain' && t.kind !== 'op')
    expect(kinds('Stop-Service -Name $s -Force')).toEqual([
      { kind: 'cmdlet', text: 'Stop-Service' },
      { kind: 'param', text: '-Name' },
      { kind: 'variable', text: '$s' },
      { kind: 'param', text: '-Force' },
    ])
    expect(kinds("'x' # note")).toEqual([
      { kind: 'string', text: "'x'" },
      { kind: 'comment', text: '# note' },
    ])
  })

  it('does not treat a hyphenated path segment or noun as a cmdlet', () => {
    expect(tokenize('C:\\a\\foo-bar', 'powershell').some((t) => t.kind === 'cmdlet')).toBe(false)
    expect(tokenize('Foo-Bar', 'powershell').some((t) => t.kind === 'cmdlet')).toBe(false)
  })

  it('does not let code hide inside a double-quoted string', () => {
    const toks = tokenize('"x $(Remove-Item -Recurse C:\\) y"', 'powershell')
    expect(toks.find((t) => t.text === 'Remove-Item')?.kind).toBe('cmdlet')
    expect(toks.find((t) => t.text === '-Recurse')?.kind).toBe('param')
    const sh = tokenize('echo "x $(rm -rf /) y"', 'shell')
    expect(sh.find((t) => t.text === '-rf')?.kind).toBe('param')
  })

  it('keeps a variable inside a double-quoted string a variable', () => {
    expect(tokenize('"a $env:windir b"', 'powershell').find((t) => t.kind === 'variable')?.text).toBe('$env:windir')
  })

  it('does not treat # inside a word as a comment', () => {
    expect(tokenize('echo a#b', 'shell').some((t) => t.kind === 'comment')).toBe(false)
  })
})

describe('splitSuspect', () => {
  it('flags invisible and bidi-control characters, keeping them in the text', () => {
    const runs = splitSuspect('a\u202Eb\u200Bc')
    expect(runs.map((r) => r.text).join('')).toBe('a\u202Eb\u200Bc')
    expect(runs.filter((r) => r.suspect).map((r) => r.suspect)).toEqual(['U+202E', 'U+200B'])
  })

  it('leaves tabs, newlines and ordinary non-ASCII alone', () => {
    expect(splitSuspect('a\tb\nü€').every((r) => !r.suspect)).toBe(true)
  })
})

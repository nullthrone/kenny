import { beforeEach, describe, expect, it } from 'vitest'
import { composerKeyAction, readEnterToSend, writeEnterToSend } from './preferences'

function key(k: string, mods: { shiftKey?: boolean; metaKey?: boolean; ctrlKey?: boolean } = {}) {
  return { key: k, shiftKey: false, metaKey: false, ctrlKey: false, ...mods }
}

beforeEach(() => {
  localStorage.clear()
})

/**
 * One preference, two composers. The Ask kenny drawer honoured it; the ticket
 * composer sent on Enter unconditionally — so the same key did different things
 * two clicks apart, and the ticket side (where the setting is offered, and where
 * replies are longest) was the one that could not be turned off.
 */
describe('composerKeyAction', () => {
  it('with the preference off, Enter inserts a newline and Cmd/Ctrl+Enter sends', () => {
    expect(composerKeyAction(key('Enter'), false)).toBe('newline')
    expect(composerKeyAction(key('Enter', { metaKey: true }), false)).toBe('send')
    expect(composerKeyAction(key('Enter', { ctrlKey: true }), false)).toBe('send')
  })

  it('with the preference on, Enter sends', () => {
    expect(composerKeyAction(key('Enter'), true)).toBe('send')
  })

  it('treats Shift+Enter as a newline in both modes, so one habit works either way', () => {
    expect(composerKeyAction(key('Enter', { shiftKey: true }), true)).toBe('newline')
    expect(composerKeyAction(key('Enter', { shiftKey: true }), false)).toBe('newline')
  })

  it('leaves every other key alone', () => {
    expect(composerKeyAction(key('a'), true)).toBe('newline')
    expect(composerKeyAction(key('Escape'), true)).toBe('newline')
  })

  it('reads the stored preference when the caller does not pass one', () => {
    expect(composerKeyAction(key('Enter'))).toBe('newline')
    writeEnterToSend(true)
    expect(composerKeyAction(key('Enter'))).toBe('send')
  })
})

describe('enter-to-send storage', () => {
  it('defaults to off — Enter must not send until somebody asks for it', () => {
    expect(readEnterToSend()).toBe(false)
  })

  it('round-trips, and keeps the legacy key format the old dashboard wrote', () => {
    writeEnterToSend(true)
    expect(localStorage.getItem('kenny-enter-send')).toBe('on')
    expect(readEnterToSend()).toBe(true)

    writeEnterToSend(false)
    expect(localStorage.getItem('kenny-enter-send')).toBe('off')
    expect(readEnterToSend()).toBe(false)
  })
})

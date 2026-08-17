import { describe, expect, it } from 'vitest'
import { createSSEFrameParser } from './sse'
import type { ChatEvent } from './types'

function parse(raw: string): ChatEvent {
  return JSON.parse(raw) as ChatEvent
}

describe('createSSEFrameParser', () => {
  it('parses a single complete frame in one push', () => {
    const parser = createSSEFrameParser(parse)
    const events = parser.push('data: {"type":"done"}\n\n')
    expect(events).toEqual([{ type: 'done' }])
  })

  it('parses multiple frames delivered in one chunk', () => {
    const parser = createSSEFrameParser(parse)
    const events = parser.push(
      'data: {"type":"text_delta","text":"a"}\n\ndata: {"type":"text_delta","text":"b"}\n\n',
    )
    expect(events).toEqual([
      { type: 'text_delta', text: 'a' },
      { type: 'text_delta', text: 'b' },
    ])
  })

  it('ignores event:/id: lines and reads only the data: line', () => {
    const parser = createSSEFrameParser(parse)
    const events = parser.push('event: message\nid: 42\ndata: {"type":"done"}\n\n')
    expect(events).toEqual([{ type: 'done' }])
  })

  it('yields nothing for a frame split across two chunks mid-token, then the event on completion', () => {
    const parser = createSSEFrameParser(parse)

    // The JSON string value itself is split mid-token ("hel" | "lo").
    const first = parser.push('data: {"type":"text_delta","text":"hel')
    expect(first).toEqual([])

    const second = parser.push('lo"}\n\n')
    expect(second).toEqual([{ type: 'text_delta', text: 'hello' }])
  })

  it('carries a partial frame across more than two chunks', () => {
    const parser = createSSEFrameParser(parse)
    expect(parser.push('data: {"typ')).toEqual([])
    expect(parser.push('e":"pen')).toEqual([])
    expect(parser.push('ding","tool":"winget_up')).toEqual([])
    const events = parser.push('date","args":{},"agent_id":"oma-pc"}\n\n')
    expect(events).toEqual([
      { type: 'pending', tool: 'winget_update', args: {}, agent_id: 'oma-pc' },
    ])
  })

  it('retains a trailing partial frame after yielding a complete one in the same chunk', () => {
    const parser = createSSEFrameParser(parse)
    const events = parser.push('data: {"type":"done"}\n\ndata: {"type":"text_de')
    expect(events).toEqual([{ type: 'done' }])
    const rest = parser.push('lta","text":"x"}\n\n')
    expect(rest).toEqual([{ type: 'text_delta', text: 'x' }])
  })
})

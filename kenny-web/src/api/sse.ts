/**
 * SSE streaming, matching the server's actual wire behaviour exactly
 * (notes/api-contract-actual.md §2), which is unusual enough to spell out:
 *
 * - The request is a POST with a JSON body, not a GET/EventSource.
 * - The SSE `event:`/`id:` lines are ignored entirely — every event's type
 *   comes from a `type` field INSIDE the JSON payload on the `data:` line.
 * - Frames are separated by a blank line (`\n\n`); within a frame, only the
 *   first line starting with `data:` is read and `JSON.parse`d.
 * - A pre-stream error (the initial response is non-2xx) is a normal JSON
 *   error body, handled the same way as `apiFetch`. A 401 is a full-page
 *   redirect, same as everywhere else. A mid-stream error is just another
 *   event, `{ type: 'error', ... }` — the generator does not throw for it.
 */
import { ApiError, redirectToLogin } from './client'
import type { ChatEvent } from './types'

/**
 * Incremental frame parser, factored out of the network loop so it can be
 * unit-tested without a fetch/ReadableStream. Feed it decoded text chunks
 * in arrival order; it returns every complete event parsed so far and
 * retains a partial frame (which may split a token, or even a whole line)
 * across calls.
 */
export function createSSEFrameParser<T>(parse: (raw: string) => T) {
  let buffer = ''
  return {
    push(chunk: string): T[] {
      buffer += chunk
      const parsed: T[] = []
      let boundary: number
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const dataLine = frame.split('\n').find((line) => line.startsWith('data:'))
        if (dataLine) parsed.push(parse(dataLine.slice(5).trim()))
      }
      return parsed
    },
  }
}

function parseChatEvent(raw: string): ChatEvent {
  return JSON.parse(raw) as ChatEvent
}

export interface StreamOptions {
  signal?: AbortSignal
}

/**
 * POSTs `body` to `url` and yields each `ChatEvent` as it arrives.
 * Used by all five streamed endpoints (chat, chat/confirm, ticket chat,
 * forecast, recommendation) — they all share this exact event vocabulary.
 */
export async function* streamChatEvents(
  url: string,
  body: unknown,
  opts?: StreamOptions,
): AsyncGenerator<ChatEvent, void, undefined> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: opts?.signal,
  })

  if (res.status === 401) {
    redirectToLogin()
    throw new ApiError('unauthorized', 401)
  }

  if (!res.ok) {
    let message = `${url} -> ${res.status}`
    try {
      const data: unknown = await res.json()
      if (
        typeof data === 'object' &&
        data !== null &&
        'error' in data &&
        typeof (data as Record<string, unknown>).error === 'string'
      ) {
        message = (data as Record<string, unknown>).error as string
      }
    } catch {
      // Non-JSON error body — keep the generic message.
    }
    throw new ApiError(message, res.status)
  }

  if (!res.body) return

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  const parser = createSSEFrameParser(parseChatEvent)

  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    const chunk = decoder.decode(value, { stream: true })
    for (const event of parser.push(chunk)) {
      yield event
    }
  }
}

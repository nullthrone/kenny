import { describe, expect, it } from 'vitest'
import shared from './aiFeatures.json'
import { AI_FEATURES } from './aiStatus'

/** The server tests `kenny_server.ai.FEATURES` against the same file. */
describe('AI feature names', () => {
  it('match the list the server switches', () => {
    expect([...AI_FEATURES]).toEqual(shared)
  })
})

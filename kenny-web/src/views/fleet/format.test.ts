import { describe, expect, it } from 'vitest'
import type { FleetAgent } from '../../api/types'
import { groupByReachability } from './format'

function host(agent_id: string, online: boolean): FleetAgent {
  return {
    agent_id,
    online,
    overall: 'ok',
    summary: '',
    severity_label: 'HEALTHY',
    os: 'linux',
    agent_version: '0.0.0',
    collected_at: null,
  }
}

describe('groupByReachability', () => {
  it('puts online hosts first and keeps the incoming order inside each group', () => {
    const groups = groupByReachability([host('a', false), host('b', true), host('c', false), host('d', true)])
    expect(groups.map((g) => g.key)).toEqual(['online', 'offline'])
    expect(groups[0].agents.map((h) => h.agent_id)).toEqual(['b', 'd'])
    expect(groups[1].agents.map((h) => h.agent_id)).toEqual(['a', 'c'])
  })

  it('omits a group with no hosts', () => {
    expect(groupByReachability([host('a', true)]).map((g) => g.key)).toEqual(['online'])
    expect(groupByReachability([host('a', false)]).map((g) => g.key)).toEqual(['offline'])
  })
})

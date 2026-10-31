import { describe, expect, it } from 'vitest'
import type { LevelOverview, OverviewNode, StateCounts } from '../src/api/graphql'
import { CONCURRENCY, overviewAt, schedule, spawnIndices, stateAt } from '../src/debug/replay'

const none: StateCounts = { queued: 0, running: 0, completed: 0, failed: 0, interrupted: 0, conflict: 0, stopped: 0 }

function overviewNode(identity: string, inputs: string[], counts: Partial<StateCounts> | null): OverviewNode {
  return {
    identity, kind: counts ? 'run_step' : 'derive', name: identity, type: { dims: [], schema: 'str' },
    params: {}, inputs: inputs.map((input) => ({ label: input, identity: input })), valueCid: null,
    version: `v/${identity}`, mapOver: [], replicaDims: [], counts: counts && { ...none, ...counts },
    activity: counts && { firstStartedAt: null, lastFinishedAt: null, finished: 0, oldestRunningSince: null },
    failures: counts && (counts.failed ? [{ typeName: 'RuntimeError', message: 'boom', count: counts.failed }] : []),
  }
}

/** `a` and `c` read the input; `b` reads `a` through a derived node. */
const final: LevelOverview = {
  pathname: 'root', workflowName: 'w', state: 'completed', resultCid: 'cid-result', error: null,
  startedAt: '2026-01-01T00:00:00.000Z', finishedAt: '2026-01-01T00:10:00.000Z',
  attemptCount: 1, returnedNode: 'b', awaitingNode: null, version: 'v', asOf: '2026-01-01T00:10:00.000Z',
  nodes: [
    overviewNode('in', [], null),
    overviewNode('a', ['in'], { completed: 18, failed: 2 }),
    overviewNode('d', ['a'], null),
    overviewNode('b', ['d'], { completed: 5 }),
    overviewNode('c', ['in'], { completed: 3 }),
  ],
}

const countsOf = (level: LevelOverview, id: string) => level.nodes.find((node) => node.identity === id)!.counts!
const sum = (counts: StateCounts) => Object.values(counts).reduce((a, b) => a + b, 0)

describe('the run replay', () => {
  const plan = schedule(final.nodes)

  it('starts with the first runs expanded, up to the concurrency limit running, and the rest waiting', () => {
    const start = overviewAt(final, plan, 0, Date.parse('2026-02-01T00:00:00Z'))
    expect(countsOf(start, 'a')).toEqual({ ...none, running: CONCURRENCY, queued: 20 - CONCURRENCY })
    expect(countsOf(start, 'c')).toEqual({ ...none, queued: 3 })
    expect(countsOf(start, 'b')).toEqual(none)
    expect(start.nodes.find((node) => node.identity === 'd')!.counts).toBeNull()
    expect(start).toMatchObject({ state: 'running', resultCid: null, finishedAt: null, returnedNode: null })
  })

  it('expands a run only once every run upstream of it has finished, and never loses an invocation', () => {
    let previousDone = 0
    for (let step = 1; step < 100; step++) {
      const level = overviewAt(final, plan, step / 100, 0)
      const a = countsOf(level, 'a')
      const b = countsOf(level, 'b')
      if (sum(b) > 0) expect(a.completed + a.failed).toBe(20)
      expect([0, 20]).toContain(sum(a))
      expect(a.failed).toBeLessThanOrEqual(2)
      const done = a.completed + a.failed + b.completed
      expect(done).toBeGreaterThanOrEqual(previousDone)
      previousDone = done
    }
  })

  it('reads each run’s activity and failures off the timeline, as of now', () => {
    const now = Date.parse('2026-02-01T00:00:00Z')
    const level = overviewAt(final, plan, 0.3, now)
    expect(level.asOf).toBe(new Date(now).toISOString())
    const a = level.nodes.find((node) => node.identity === 'a')!
    const { counts, activity, failures } = a
    expect(activity!.finished).toBe(counts!.completed + counts!.failed)
    if (counts!.running > 0) expect(Date.parse(activity!.oldestRunningSince!)).toBeLessThanOrEqual(now)
    expect(Date.parse(activity!.firstStartedAt!)).toBeLessThanOrEqual(Date.parse(activity!.lastFinishedAt ?? activity!.firstStartedAt!))
    expect(failures!.reduce((total, group) => total + group.count, 0)).toBe(counts!.failed)
    const start = overviewAt(final, plan, 0, now).nodes.find((node) => node.identity === 'b')!
    expect(start.activity).toEqual({ firstStartedAt: null, lastFinishedAt: null, finished: 0, oldestRunningSince: null })
  })

  it('ends at the recorded overview', () => {
    expect(overviewAt(final, plan, 1, 0)).toBe(final)
    expect(countsOf(overviewAt(final, plan, 0.999999, 0), 'b').completed).toBeLessThanOrEqual(5)
  })

  it('is the same every time', () => {
    expect(schedule(final.nodes).makespan).toBe(plan.makespan)
  })

  it('dates the start as long ago as the recorded run had been going', () => {
    const now = Date.parse('2026-02-01T00:00:00Z')
    expect(overviewAt(final, plan, 0.5, now).startedAt).toBe(new Date(now - 5 * 60_000).toISOString())
  })

  it('changes a node version only with its counts', () => {
    const early = overviewAt(final, plan, 0, 0)
    const later = overviewAt(final, plan, 0.001, 0)
    expect(later.nodes.find((n) => n.identity === 'b')!.version).toBe(early.nodes.find((n) => n.identity === 'b')!.version)
    expect(later.version).not.toBe(early.version)
  })

  it('places each recorded invocation at an index recorded in its state', () => {
    const timeline = plan.runs.get('a')!
    const states = [...Array(18).fill('completed'), 'failed', 'failed']
    const indices = spawnIndices(timeline, states)
    expect(new Set(indices).size).toBe(20)
    indices.forEach((index, i) => expect(stateAt(timeline, index, plan.makespan)).toBe(states[i]))
    expect(() => spawnIndices(timeline, Array(3).fill('failed'))).toThrow()
  })
})

describe('a replayed failure', () => {
  const plain = schedule(final.nodes)
  const failing = schedule(final.nodes, 0.2)

  it('fails the busiest run from that point, and never expands the runs reading it', () => {
    expect(failing.failure?.node).toBe('a')
    expect(failing.makespan).toBeLessThan(plain.makespan)
    for (let step = 0; step <= 100; step++) expect(countsOf(overviewAt(final, failing, step / 100, 0), 'b')).toEqual(none)
    const end = overviewAt(final, failing, 1, 0)
    expect(end).toMatchObject({ state: 'failed', resultCid: null, returnedNode: null })
    expect(countsOf(end, 'a').failed).toBeGreaterThan(2)
    expect(countsOf(end, 'c')).toEqual({ ...none, completed: 3 })
  })

  it('agrees with the plain replay up to the failure', () => {
    for (const progress of [0, 0.05, 0.1, 0.19]) {
      const time = progress * plain.makespan
      const before = overviewAt(final, plain, progress, 0)
      const after = overviewAt(final, failing, time / failing.makespan, 0)
      for (const id of ['a', 'b', 'c']) expect(countsOf(after, id)).toEqual(countsOf(before, id))
    }
  })
})

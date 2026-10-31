import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { GqlInvocation } from '../src/api/graphql'
import { inProgress, node, overviewOf } from './fixtures'

/** A stand-in for the debug_web backend: what it serves, and what it was asked. */
class Backend {
  version = 'v1'
  invocation: GqlInvocation = inProgress()
  stepReads = 0
  stepState = 'failed'
  failSteps = false
  failVersions = false
  calls = { versions: 0, invocation: 0, array: 0, overview: 0 }

  fetch = async (_url: string, init: { body: string }) => {
    const { query, variables } = JSON.parse(init.body) as { query: string; variables: Record<string, unknown> }
    if (query.includes('levelOverview')) {
      this.calls.overview += 1
      if (this.failVersions) throw new Error('backend down')
      return reply({levelOverview: overviewOf(structuredClone(this.invocation), this.version)})
    }
    if (query.includes('invocationVersion')) {
      this.calls.versions += 1
      if (this.failVersions) throw new Error('backend down')
      const data: Record<string, string> = {}
      for (const name of Object.keys(variables)) if (name.startsWith('p')) data[`v${name.slice(1)}`] = this.version
      return reply(data)
    }
    if (query.includes('invocation(')) {
      this.calls.invocation += 1
      return reply({ invocation: structuredClone(this.invocation) })
    }
    if (query.includes('step(')) {
      this.stepReads += 1
      if (this.failSteps) throw new Error('step temporarily unavailable')
      return reply({step: {stepName: 'roll', requestCid: 'request', state: this.stepState,
        resultCid: null, attempt: null, logs: []}})
    }
    if (query.includes('array(')) {
      this.calls.array += 1
      throw new Error('trace must not fetch full arrays')
    }
    throw new Error(`unexpected query ${query}`)
  }
}

function reply(data: unknown) {
  return { ok: true, json: async () => ({ data }) }
}

let backend: Backend
let live: typeof import('../src/api/live')

beforeEach(async () => {
  vi.useFakeTimers()
  vi.resetModules()
  backend = new Backend()
  vi.stubGlobal('fetch', backend.fetch)
  vi.stubGlobal('window', globalThis)
  vi.stubGlobal('document', { hidden: false })
  live = await import('../src/api/live')
})

afterEach(() => {
  live.stopPolling('job')
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

/** Load the root level and watch it, as a mounted job view would. */
async function watchRoot() {
  const invocation = live.overviewEntry('job', 'root')
  invocation.watchers += 1
  const level = live.levelEntry('job', 'root')
  await vi.advanceTimersByTimeAsync(0)
  expect(level.state.status).toBe('ready')
  return { invocation, level }
}

describe('the live store', () => {
  it('publishes the graph without requesting outputs or full invocation records', async () => {
    backend.invocation.spawns = Array.from({ length: 100_000 }, (_, i) => ({
      ...inProgress().spawns[0], key: [String(i)], resultCid: `output-${i}`,
    }))
    const { level } = await watchRoot()
    expect(level.value!.steps.find((step) => step.id === 'roll')!.progress.counts?.total).toBe(100_000)
    expect(level.value!.steps.every((step) => !('rows' in step) && !('invocations' in step))).toBe(true)
    expect(backend.calls).toEqual({ versions: 0, invocation: 0, array: 0, overview: 1 })
  })

  it('publishes only changed summaries and keeps node order without loading new outputs', async () => {
    // Two independent sources: their dependency order is either way, so only memory fixes it.
    const sources = [
      node('s1', 'source_array', { valueCid: 'cid-a' }),
      node('s2', 'source_array', { valueCid: 'cid-a' }),
    ]
    backend.invocation = { ...inProgress(), nodes: [...sources, ...inProgress().nodes] }
    const { invocation, level } = await watchRoot()
    live.startPolling('job')
    await vi.advanceTimersByTimeAsync(0)
    expect(backend.calls.invocation).toBe(0)
    expect(live.liveStatus().polling).toBe(true)

    const previous = invocation.value
    await vi.advanceTimersByTimeAsync(live.POLL_INTERVAL)
    expect(invocation.value).toBe(previous)

    backend.version = 'v2'
    backend.invocation = {
      ...inProgress(),
      nodes: [...sources, ...inProgress().nodes].reverse(),
      spawns: [inProgress().spawns[0], { ...inProgress().spawns[1], state: 'completed', resultCid: 'cid-b' }],
    }
    await vi.advanceTimersByTimeAsync(live.POLL_INTERVAL)
    expect(backend.calls.invocation).toBe(0)
    expect(backend.calls.array).toBe(0)
    expect(invocation.value!.version).toBe('v2')
    expect(invocation.value!.invocation.nodes.map((n) => n.identity)).toEqual(['s1', 's2', 'in', 'roll', 'grade'])
    const roll = level.value!.steps.find((step) => step.id === 'roll')!
    expect(roll).not.toHaveProperty('rows')
    expect(roll.progress.counts).toMatchObject({ done: 2, total: 2 })
  })

  it('stops polling once nothing watched is live', async () => {
    await watchRoot()
    live.startPolling('job')
    await vi.advanceTimersByTimeAsync(0)
    backend.version = 'v3'
    backend.invocation = {
      ...inProgress(),
      state: 'completed',
      resultCid: 'cid-a',
      spawns: inProgress().spawns.map((s) => ({ ...s, state: 'completed', resultCid: 'cid-a' })),
    }
    await vi.advanceTimersByTimeAsync(live.POLL_INTERVAL)
    await vi.advanceTimersByTimeAsync(live.POLL_INTERVAL)
    expect(live.liveStatus().polling).toBe(false)
    const asked = backend.calls.overview
    await vi.advanceTimersByTimeAsync(live.POLL_INTERVAL * 5)
    expect(backend.calls.overview).toBe(asked)

    // Something outside the records (the job list) says the job runs again: one forced poll.
    live.pokeLive()
    await vi.advanceTimersByTimeAsync(0)
    expect(backend.calls.overview).toBe(asked + 1)
  })

  it('keeps a settled job’s forced refresh pending while the tab is hidden', async () => {
    backend.invocation = inProgress({state: 'interrupted', spawns: [], attempts: []})
    const {invocation} = await watchRoot()
    live.startPolling('job')
    await vi.advanceTimersByTimeAsync(0)
    expect(live.liveStatus().polling).toBe(false)
    const asked = backend.calls.overview

    vi.stubGlobal('document', {hidden: true})
    backend.version = 'resumed'
    backend.invocation = {...backend.invocation, state: 'completed', resultCid: 'cid-a'}
    live.pokeLive()
    await vi.advanceTimersByTimeAsync(live.POLL_INTERVAL * 3)
    expect(backend.calls.overview).toBe(asked)

    vi.stubGlobal('document', {hidden: false})
    await vi.advanceTimersByTimeAsync(live.POLL_INTERVAL)
    expect(invocation.value!.version).toBe('resumed')
    expect(backend.calls.overview).toBe(asked + 1)
    await vi.advanceTimersByTimeAsync(live.POLL_INTERVAL * 3)
    expect(live.liveStatus().polling).toBe(false)
    expect(backend.calls.overview).toBe(asked + 1)
  })

  it('polls at once when poked between polls, and again right after one in flight', async () => {
    await watchRoot()
    live.startPolling('job')
    await vi.advanceTimersByTimeAsync(0)
    const asked = backend.calls.overview
    live.pokeLive()
    await vi.advanceTimersByTimeAsync(0)
    expect(backend.calls.overview).toBe(asked + 1)

    let answer: () => void = () => {}
    const fetch = backend.fetch
    vi.stubGlobal('fetch', (url: string, init: { body: string }) =>
      new Promise((resolve) => (answer = () => resolve(fetch(url, init)))))
    await vi.advanceTimersByTimeAsync(live.POLL_INTERVAL)
    live.pokeLive()
    vi.stubGlobal('fetch', backend.fetch)
    answer()
    await vi.advanceTimersByTimeAsync(0)
    expect(backend.calls.overview).toBe(asked + 3)
  })

  it('keeps the data and reports reconnecting when a poll fails', async () => {
    const { invocation } = await watchRoot()
    live.startPolling('job')
    await vi.advanceTimersByTimeAsync(0)
    backend.failVersions = true
    await vi.advanceTimersByTimeAsync(live.POLL_INTERVAL)
    expect(live.liveStatus().reconnecting).toBe(true)
    expect(invocation.state.status).toBe('ready')
    backend.failVersions = false
    await vi.advanceTimersByTimeAsync(10_000)
    expect(live.liveStatus().reconnecting).toBe(false)
  })
})


it.each(['failed', 'conflict', 'stopped'])('refreshes a watched %s pin when an overview resumes', async state => {
  backend.stepState = state
  backend.invocation = inProgress({state: 'interrupted',
    spawns: [{...inProgress().spawns[1], state}], attempts: []})
  const {invocation} = await watchRoot()
  const step = live.stepEntry('job', 'k/b')
  step.watchers++
  await vi.advanceTimersByTimeAsync(0)
  expect(step.value!.state).toBe(state)
  live.startPolling('job')
  await vi.advanceTimersByTimeAsync(0)
  expect(live.liveStatus().polling).toBe(false)

  backend.version = 'resumed'
  backend.stepState = 'running'
  backend.invocation = inProgress()
  live.pokeLive()
  await vi.advanceTimersByTimeAsync(0)
  expect(invocation.value!.version).toBe('resumed')
  expect(step.value!.state).toBe('running')
  expect(backend.stepReads).toBe(2)
  expect(backend.calls.invocation).toBe(0)
})

it('refreshes a stopped pin when a live overview changes without a forced poll', async () => {
  backend.stepState = 'stopped'
  await watchRoot()
  const step = live.stepEntry('job', 'k/b')
  step.watchers++
  live.startPolling('job')
  await vi.advanceTimersByTimeAsync(0)
  const reads = backend.stepReads
  backend.version = 'changed'
  backend.stepState = 'running'
  await vi.advanceTimersByTimeAsync(live.POLL_INTERVAL)
  expect(step.value!.state).toBe('running')
  expect(backend.stepReads).toBe(reads + 1)
  expect(backend.calls.invocation).toBe(0)
})

it('retries a failed pin refresh even after publishing its changed overview', async () => {
  backend.invocation = inProgress({state: 'interrupted', attempts: [],
    spawns: [{...inProgress().spawns[1], state: 'failed'}]})
  const {invocation} = await watchRoot()
  const step = live.stepEntry('job', 'k/b')
  step.watchers++
  live.startPolling('job')
  await vi.advanceTimersByTimeAsync(0)

  backend.version = 'resumed'
  backend.invocation = inProgress()
  backend.stepState = 'running'
  backend.failSteps = true
  live.pokeLive()
  await vi.advanceTimersByTimeAsync(0)
  expect(invocation.value!.version).toBe('resumed')
  expect(step.value!.state).toBe('failed')
  expect(live.liveStatus().reconnecting).toBe(true)

  backend.failSteps = false
  await vi.advanceTimersByTimeAsync(5000)
  expect(step.value!.state).toBe('running')
  expect(backend.stepReads).toBe(3)
  expect(live.liveStatus().reconnecting).toBe(false)
})

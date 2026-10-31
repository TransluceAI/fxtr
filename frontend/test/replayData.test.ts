import { afterEach, describe, expect, it } from 'vitest'
import type { GqlSpawn, LevelOverview, OverviewNode, StateCounts } from '../src/api/graphql'
import { OUTPUT_PAGE, RUN_PAGE, type OutputPage } from '../src/api/browse'
import { clearReplayData, replay, replayMiddleware } from '../src/debug/replayData'

const none: StateCounts = { queued: 0, running: 0, completed: 0, failed: 0, interrupted: 0, conflict: 0, stopped: 0 }

function run(identity: string, inputs: string[], counts: Partial<StateCounts>): OverviewNode {
  return {
    identity, kind: 'run_step', name: identity, type: { dims: [['seed', 'str']], schema: 'str' }, params: {},
    inputs: inputs.map((input) => ({ label: input, identity: input })), valueCid: null, version: `v/${identity}`,
    mapOver: ['seed'], replicaDims: [], counts: { ...none, ...counts },
  }
}

/** `a` runs 20 seeds, two of which failed; `b` reads `a` and ran 20. */
const recorded: LevelOverview = {
  pathname: 'root', workflowName: 'w', state: 'completed', resultCid: 'cid-result', error: null,
  startedAt: '2026-01-01T00:00:00.000Z', finishedAt: '2026-01-01T00:10:00.000Z',
  attemptCount: 1, returnedNode: 'b', awaitingNode: null, version: 'v',
  nodes: [run('a', [], { completed: 18, failed: 2 }), run('b', ['a'], { completed: 20 })],
}

const seeds = Array.from({ length: 20 }, (_, i) => `s${String(i).padStart(2, '0')}`)
const spawnsOf = (nodeId: string): GqlSpawn[] => seeds.map((seed, i) => ({
  nodeIdentity: nodeId, key: [seed], childPathname: null, stepCacheKey: `${nodeId}/${seed}`,
  resultCid: nodeId === 'a' && i >= 18 ? null : `cid-${seed}`, state: nodeId === 'a' && i >= 18 ? 'failed' : 'completed',
}))

/** The backend: the recorded overview, run pages, and one output row per completed invocation. */
async function send(query: string, variables: Record<string, unknown>): Promise<unknown> {
  if (query.includes('levelOverview(')) return { levelOverview: recorded }
  if (query === RUN_PAGE) {
    const spawns = spawnsOf(variables.nodeId as string)
    const start = variables.after === null ? 0 : Number(variables.after)
    const end = start + (variables.first as number)
    return { runPage: { items: spawns.slice(start, end), total: spawns.length, next: end < spawns.length ? String(end) : null } }
  }
  if (query === OUTPUT_PAGE) {
    const source = variables.source as { nodeId: string }
    const rows = spawnsOf(source.nodeId).filter((spawn) => spawn.state === 'completed').map((spawn) => ({ key: spawn.key, value: '' }))
    const offset = variables.offset as number
    const first = variables.first as number
    return { outputPage: { type: { dims: [['seed', 'str']], schema: 'str' }, rows: rows.slice(offset, offset + first),
      total: rows.length, offset, hasMore: offset + first < rows.length } }
  }
  throw new Error(`unexpected query ${query}`)
}

const ask = (query: string, variables: Record<string, unknown>) =>
  replayMiddleware(query, variables, new AbortController().signal, send) as Promise<Record<string, never>>

async function at(progress: number, failAt: number | null = null) {
  replay.set({ jobId: 'job', progress, playing: false, speed: 1, failAt })
  const { levelOverview } = await ask('query { levelOverview(jobId: $jobId) }', { jobId: 'job', pathname: 'root' }) as { levelOverview: LevelOverview }
  const counts = (id: string) => levelOverview.nodes.find((node) => node.identity === id)!.counts!
  const runPage = async (nodeId: string, state: string | null = null) =>
    (await ask(RUN_PAGE, { jobId: 'job', pathname: 'root', nodeId, selection: {}, after: null, first: 200, state, unfinished: false }) as
      { runPage: { items: GqlSpawn[]; total: number } }).runPage
  const outputPage = async (nodeId: string) =>
    (await ask(OUTPUT_PAGE, { source: { jobId: 'job', pathname: 'root', nodeId }, offset: 0, first: 25, filters: [], sort: null, last: false }) as
      { outputPage: OutputPage }).outputPage
  return { levelOverview, counts, runPage, outputPage }
}

afterEach(() => {
  replay.set(null)
  clearReplayData()
})

describe('the replayed job', () => {
  it('lists only the invocations that exist, and the rows of those that completed, by then', async () => {
    for (const progress of [0, 0.3, 0.6, 1]) {
      const level = await at(progress)
      for (const id of ['a', 'b']) {
        const counts = level.counts(id)
        const total = Object.values(counts).reduce((x, y) => x + y, 0)
        expect((await level.runPage(id)).total).toBe(total)
        expect((await level.runPage(id, 'completed')).total).toBe(counts.completed)
        const page = await level.outputPage(id)
        expect(page.total).toBe(counts.completed)
        for (const row of page.rows)
          expect((await level.runPage(id)).items.find((spawn) => spawn.key[0] === row.key[0])!.state).toBe('completed')
      }
    }
    expect((await (await at(0)).runPage('b')).total).toBe(0)
    expect((await (await at(1)).outputPage('a')).total).toBe(18)
  })

  it('shows a failure in the invocations and rows too', async () => {
    const level = await at(1, 0.2)
    expect(level.levelOverview.state).toBe('failed')
    expect((await level.runPage('a', 'failed')).total).toBe(level.counts('a').failed)
    expect((await level.outputPage('a')).total).toBe(level.counts('a').completed)
    expect((await level.runPage('b')).total).toBe(0)
  })

  it('passes other jobs through', async () => {
    replay.set({ jobId: 'job', progress: 0, playing: false, speed: 1, failAt: null })
    const { levelOverview } = await ask('query { levelOverview(jobId: $jobId) }', { jobId: 'other', pathname: 'root' }) as { levelOverview: LevelOverview }
    expect(levelOverview).toBe(recorded)
  })
})

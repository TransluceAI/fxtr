/**
 * What the run replay answers in the backend's place: its state, and the request middleware that
 * makes every view of the replayed job agree with it. Development only (see `RunReplay.tsx`).
 *
 * Each level of the job replays on its own schedule (`replay.ts`), built from its recorded
 * overview. A run's recorded invocations, read once in the backend's key order, each take a place
 * in their node's timeline (`spawnIndices`), so every invocation has a state at every point of the
 * replay, and every read about a run is answered from those states:
 *
 * - `levelOverview`: counts and the level's own state (`overviewAt`).
 * - `runPage`: the invocations that exist by then, in their replayed states, filtered and paged
 *   here (and `fetchFirstSpawns`' batch of them, one selection each).
 * - `invocationPage`: the backend's page, less the invocations that do not exist by then, the
 *   others in their replayed states, with no result before they complete. Its filters, order and
 *   total are the recorded run's.
 * - `keyPage`: the backend's values, with counts of the replayed states; none before the run
 *   expands.
 * - `outputPage`: every row the backend's filters and sort pick, read once in full, less the rows
 *   of invocations not completed by then, paged here the way the backend pages.
 * - `step`: the pin in its replayed state (no attempt while queued, no result before it completes).
 * - `invocation` / `invocationVersion`: the complete record's spawns in their replayed states, and a
 *   version that moves with the replay.
 * - `jobSummary` / `jobSummaries`: running until the end, failed once the failure has happened.
 *
 * What a finished job recorded never changes, so everything read from the backend is kept until
 * the replay stops. `outputDimValues` is left alone: the distinct values of a dim produced inside
 * invocations are the recorded ones.
 */

import {
  LEVEL_OVERVIEW,
  type GqlInvocation,
  type GqlSpawn,
  type GqlProvenance,
  type GqlStepPin,
  type LevelOverview,
  type Page,
  type RequestMiddleware,
  type Send,
  type StateCounts,
} from '../api/graphql'
import { OUTPUT_PAGE, RUN_PAGE, type InvocationPage, type KeyItem, type OutputPage, type RunPage } from '../api/browse'
import { Store } from '../lib/store'
import type { JobSummary, KeyValue } from '../types'
import { overviewAt, replayedTime, schedule, spawnIndices, stateAt, timeAt, type CountedState, type NodeTimeline, type Schedule } from './replay'

export interface Replay {
  jobId: string
  /** How far through the run the replay is, from 0 to 1. */
  progress: number
  playing: boolean
  speed: number
  /** Where a run fails, as a share of the way through the schedule without the failure; null for
   *  none. */
  failAt: number | null
}

export const replay = new Store<Replay | null>(null)

/** The recorded overview of the level a replayed overview was last answered for: the level the
 *  job's summary and a change of failure go by. */
let lastLevel: { jobId: string; final: LevelOverview } | null = null

const overviews = new Map<string, Promise<LevelOverview>>()
const plans = new Map<string, Schedule>()
const runs = new Map<string, Promise<RecordedRun>>()
const outputs = new Map<string, Promise<{ type: OutputPage['type']; rows: OutputPage['rows'] }>>()
/** Every step cache key read in a run, with where its invocation sits. */
const stepKeys = new Map<string, { jobId: string; pathname: string; nodeId: string; index: number }>()

/** Forget everything read for the replay. */
export function clearReplayData() {
  lastLevel = null
  for (const cache of [overviews, plans, runs, outputs, stepKeys]) cache.clear()
}

/** A run's recorded invocations in the backend's key order, with each one's timeline index. */
interface RecordedRun {
  spawns: GqlSpawn[]
  indices: number[]
  indexOfKey: Map<string, number>
}

const keyOf = (values: readonly unknown[]) => JSON.stringify(values)
const levelKey = (jobId: string, pathname: string) => `${jobId}\u0000${pathname}`

/** Reads that outlive the request that started them, since they are kept. */
const kept = () => new AbortController().signal

function remember<T>(cache: Map<string, Promise<T>>, key: string, load: () => Promise<T>): Promise<T> {
  let found = cache.get(key)
  if (!found) {
    found = load()
    cache.set(key, found)
    found.catch(() => {
      if (cache.get(key) === found) cache.delete(key)
    })
  }
  return found
}

function recordedOverview(jobId: string, pathname: string, send: Send): Promise<LevelOverview> {
  return remember(overviews, levelKey(jobId, pathname), async () => {
    const { levelOverview } = (await send(LEVEL_OVERVIEW, { jobId, pathname }, kept())) as { levelOverview: LevelOverview | null }
    if (!levelOverview) throw new Error(`no invocation ${pathname} in job ${jobId}`)
    return levelOverview
  })
}

function planOf(jobId: string, final: LevelOverview, failAt: number | null): Schedule {
  const key = `${levelKey(jobId, final.pathname)}\u0000${final.version}\u0000${failAt}`
  let plan = plans.get(key)
  if (!plan) {
    plan = schedule(final.nodes, failAt)
    plans.set(key, plan)
  }
  return plan
}

/** The plan the replay is on for `final`. */
const currentPlan = (state: Replay, final: LevelOverview) => planOf(state.jobId, final, state.failAt)

function recordedRun(jobId: string, pathname: string, nodeId: string, timeline: NodeTimeline, send: Send) {
  return remember(runs, `${levelKey(jobId, pathname)}\u0000${nodeId}`, async () => {
    const spawns: GqlSpawn[] = []
    let after: string | null = null
    do {
      const { runPage } = (await send(
        RUN_PAGE,
        { jobId, pathname, nodeId, selection: {}, after, first: 200, state: null, unfinished: false },
        kept(),
      )) as { runPage: Page<GqlSpawn> }
      spawns.push(...runPage.items)
      after = runPage.next
    } while (after !== null)
    // A schedule's recorded states are the same with or without a failure, so any plan's
    // timeline places the invocations.
    const indices = spawnIndices(timeline, spawns.map((spawn) => spawn.state))
    const indexOfKey = new Map(spawns.map((spawn, i) => [keyOf(spawn.key), indices[i]]))
    spawns.forEach((spawn, i) => {
      if (spawn.stepCacheKey !== null) stepKeys.set(spawn.stepCacheKey, { jobId, pathname, nodeId, index: indices[i] })
    })
    return { spawns, indices, indexOfKey }
  })
}

/** Everything about one run of the replayed job at the replay's current point. */
async function runAt(state: Replay, pathname: string, nodeId: string, send: Send) {
  const final = await recordedOverview(state.jobId, pathname, send)
  const node = final.nodes.find((each) => each.identity === nodeId)
  if (!node || node.counts === null) throw new Error(`no run ${nodeId} in ${pathname}`)
  const plan = currentPlan(state, final)
  const timeline = plan.runs.get(nodeId)!
  const recorded = await recordedRun(state.jobId, pathname, nodeId, timeline, send)
  const at = timeAt(plan, state.progress)
  return {
    final,
    plan,
    timeline,
    at,
    dims: [...node.mapOver, ...node.replicaDims],
    recorded,
    stateOf: (index: number) => stateAt(timeline, index, at),
  }
}

const matches = (key: readonly KeyValue[], dims: readonly string[], selection: Record<string, KeyValue>) =>
  Object.entries(selection).every(([name, value]) => key[dims.indexOf(name)] === value)

function replayedSpawn(spawn: GqlSpawn, state: CountedState): GqlSpawn {
  return { ...spawn, state, resultCid: state === 'completed' ? spawn.resultCid : null }
}

const OPERATION = /\b(levelOverview|runPage|keyPage|outputPage|invocationPage|invocationVersion|invocation|step|jobSummaries|jobSummary)\(/

export const replayMiddleware: RequestMiddleware = async (query, variables, signal, send) => {
  const state = replay.get()
  const operation = OPERATION.exec(query)?.[1]
  if (state === null || operation === undefined) return send(query, variables, signal)
  const source = variables.source as { jobId?: string; pathname?: string; nodeId?: string; cid?: string } | undefined
  const jobId = variables.jobId ?? variables.j ?? source?.jobId
  if (operation === 'jobSummary' || operation === 'jobSummaries') {
    const data = (await send(query, variables, signal)) as { jobSummary?: JobSummary | null; jobSummaries?: Page<JobSummary> }
    if (data.jobSummary) return { ...data, jobSummary: summaryAt(data.jobSummary, state) }
    if (data.jobSummaries)
      return { ...data, jobSummaries: { ...data.jobSummaries, items: data.jobSummaries.items.map((job) => summaryAt(job, state)) } }
    return data
  }
  if (jobId !== state.jobId) return send(query, variables, signal)
  switch (operation) {
    case 'levelOverview': {
      const pathname = variables.pathname as string
      const final = await recordedOverview(state.jobId, pathname, send)
      lastLevel = { jobId: state.jobId, final }
      return { levelOverview: overviewAt(final, currentPlan(state, final), state.progress, Date.now()) }
    }
    case 'runPage':
      // `fetchFirstSpawns`' batch: a first invocation `r<i>` for each selection `s<i>`.
      if (!('selection' in variables)) return firstSpawns(state, variables, send)
      return { runPage: await runPage(state, variables, send) }
    case 'keyPage':
      return { keyPage: await keyPage(state, query, variables, signal, send) }
    case 'invocationPage':
      return { invocationPage: await invocationPage(state, query, variables, signal, send) }
    case 'outputPage':
      if (source?.cid !== undefined && source.cid !== null) return send(query, variables, signal)
      return { outputPage: await outputPage(state, variables, send) }
    case 'step':
      return stepPin(state, query, variables, signal, send)
    case 'invocation':
      return invocationRecord(state, query, variables, signal, send)
    case 'invocationVersion': {
      const data = (await send(query, variables, signal)) as Record<string, string | null>
      return Object.fromEntries(Object.entries(data).map(([alias, version]) =>
        [alias, version === null ? null : `${version}:replay:${state.failAt}:${state.progress}`]))
    }
  }
  return send(query, variables, signal)
}

function summaryAt(job: JobSummary, state: Replay): JobSummary {
  if (job.id !== state.jobId) return job
  if (lastLevel?.jobId !== state.jobId) return state.progress < 1 ? { ...job, state: 'running' } : job
  const level = overviewAt(lastLevel.final, currentPlan(state, lastLevel.final), state.progress, Date.now())
  if (level === lastLevel.final) return job
  const failed = level.nodes.some((node) => (node.counts?.failed ?? 0) > 0)
  return { ...job, state: level.state === 'running' ? 'running' : 'stopped', failed }
}

async function runPage(state: Replay, variables: Record<string, unknown>, send: Send): Promise<RunPage> {
  const run = await runAt(state, variables.pathname as string, variables.nodeId as string, send)
  const selection = variables.selection as Record<string, KeyValue>
  const only = variables.state as string | null
  const unfinished = variables.unfinished as boolean
  const listed: GqlSpawn[] = []
  run.recorded.spawns.forEach((spawn, i) => {
    const now = run.stateOf(run.recorded.indices[i])
    if (now === null || !matches(spawn.key, run.dims, selection)) return
    if (only && now !== only) return
    if (unfinished && now === 'completed') return
    listed.push(replayedSpawn(spawn, now))
  })
  const after = variables.after as string | null
  const first = variables.first as number
  const asked = variables.offset as number | null | undefined
  // An offset opens the page holding it, the last page for one past the end, as the backend's.
  const offset = asked === undefined || asked === null ? null
    : Math.min(Math.floor(asked / first) * first, Math.max(0, Math.floor((listed.length - 1) / first) * first))
  const start = offset ?? (after?.startsWith('replay:') ? Number(after.slice('replay:'.length)) : 0)
  const end = start + first
  return { items: listed.slice(start, end), total: listed.length, offset, next: end < listed.length ? `replay:${end}` : null }
}

async function firstSpawns(state: Replay, variables: Record<string, unknown>, send: Send): Promise<Record<string, { items: GqlSpawn[] }>> {
  const selections = Object.entries(variables).filter(([name]) => /^s\d+$/.test(name))
  return Object.fromEntries(await Promise.all(selections.map(async ([name, selection]) => {
    const page = await runPage(state, { ...variables, selection, first: 1, after: null, state: null, unfinished: false }, send)
    return [`r${name.slice(1)}`, { items: page.items }] as const
  })))
}

async function invocationPage(
  state: Replay, query: string, variables: Record<string, unknown>, signal: AbortSignal, send: Send,
): Promise<InvocationPage> {
  const run = await runAt(state, variables.pathname as string, variables.nodeId as string, send)
  const { invocationPage: recorded } = (await send(query, variables, signal)) as { invocationPage: InvocationPage }
  const items = recorded.items.flatMap((item) => {
    const index = run.recorded.indexOfKey.get(keyOf(item.spawn.key))
    if (index === undefined) throw new Error(`a spawn of ${variables.nodeId} is not in its run's pages`)
    const now = run.stateOf(index)
    if (now === null) return []
    const spawn = replayedSpawn(item.spawn, now)
    return [now === 'completed' ? { ...item, spawn } : { spawn, rowCount: null, value: null }]
  })
  return { ...recorded, items }
}

async function keyPage(
  state: Replay, query: string, variables: Record<string, unknown>, signal: AbortSignal, send: Send,
): Promise<Page<KeyItem> & { total: number; offset?: number | null }> {
  const run = await runAt(state, variables.pathname as string, variables.nodeId as string, send)
  if (run.at < run.timeline.ready) return { items: [], total: 0, next: null, offset: (variables.offset as number | undefined) ?? null }
  const { keyPage: recorded } = (await send(query, variables, signal)) as { keyPage: Page<KeyItem> & { total: number } }
  const dim = variables.dim as string
  const position = run.dims.indexOf(dim)
  const others = Object.fromEntries(Object.entries(variables.selection as Record<string, KeyValue>).filter(([name]) => name !== dim))
  const items = recorded.items.map((item) => {
    const all: CountedState[] = []
    const matching: CountedState[] = []
    run.recorded.spawns.forEach((spawn, i) => {
      if (spawn.key[position] !== item.value) return
      const now = run.stateOf(run.recorded.indices[i])!
      all.push(now)
      if (matches(spawn.key, run.dims, others)) matching.push(now)
    })
    return { ...item, compatible: matching.length > 0, counts: countStates(matching.length > 0 ? matching : all) }
  })
  return { ...recorded, items }
}

function countStates(states: readonly CountedState[]): StateCounts {
  const counts: StateCounts = { queued: 0, running: 0, completed: 0, failed: 0, interrupted: 0, conflict: 0, stopped: 0 }
  for (const state of states) counts[state] += 1
  return counts
}

async function outputPage(state: Replay, variables: Record<string, unknown>, send: Send): Promise<OutputPage> {
  const source = variables.source as { jobId: string; pathname: string; nodeId: string }
  const run = await runAt(state, source.pathname, source.nodeId, send)
  const { filters, sort } = variables
  const all = await remember(outputs, JSON.stringify({ source, filters, sort }), async () => {
    const rows: OutputPage['rows'] = []
    let type: OutputPage['type'] = null
    for (let offset = 0; ; offset += 200) {
      const { outputPage: page } = (await send(OUTPUT_PAGE, { source, offset, first: 200, filters, sort, last: false }, kept())) as {
        outputPage: OutputPage
      }
      type ??= page.type
      rows.push(...page.rows)
      if (!page.hasMore) return { type, rows }
    }
  })
  const positions = all.type === null ? [] : run.dims.map((dim) => {
    const position = all.type!.dims.findIndex(([name]) => name === dim)
    if (position === -1) throw new Error(`the output of ${source.nodeId} has no ${dim} dim`)
    return position
  })
  const visible = all.rows.filter((row) => {
    const index = run.recorded.indexOfKey.get(keyOf(positions.map((position) => (row.key as KeyValue[])[position])))
    if (index === undefined) throw new Error(`an output row of ${source.nodeId} names no recorded invocation`)
    return run.stateOf(index) === 'completed'
  })
  const first = variables.first as number
  const count = visible.length
  let offset = variables.offset as number
  if (variables.last || offset >= count) offset = Math.max(0, Math.floor((count - 1) / first) * first)
  return { type: all.type, rows: visible.slice(offset, offset + first), total: count, offset, hasMore: count > offset + first }
}

async function stepPin(
  state: Replay, query: string, variables: Record<string, unknown>, signal: AbortSignal, send: Send,
): Promise<unknown> {
  const data = (await send(query, variables, signal)) as { step: GqlStepPin | null }
  const place = stepKeys.get(variables.cacheKey as string)
  if (!data.step || !place) return data
  const run = await runAt(state, place.pathname, place.nodeId, send)
  const pin = data.step
  const now = run.stateOf(place.index)
  if (now === null || now === 'queued')
    return { step: { ...pin, state: 'queued', resultCid: null, attempt: null, logs: [] } }
  const startedAt = replayedTime(run.final, run.plan, run.at, run.timeline.starts[place.index], Date.now())
  if (now === 'running')
    return {
      step: {
        ...pin,
        state: 'running',
        resultCid: null,
        provenance: null,
        attempt: { state: 'running', startedAt, finishedAt: null, error: null, provenance: recordedProvenance(pin) },
      },
    }
  if (now === 'failed' && pin.state !== 'failed')
    return {
      step: {
        ...pin,
        state: 'failed',
        resultCid: null,
        provenance: null,
        attempt: {
          state: 'failed',
          startedAt,
          finishedAt: replayedTime(run.final, run.plan, run.at, run.timeline.endOf[place.index], Date.now()),
          error: { typeName: 'ReplayedFailure', message: 'failed in the replay' },
          provenance: recordedProvenance(pin),
        },
      },
    }
  return data
}

/** The code a recorded step ran, which its replayed attempt is said to run too. */
function recordedProvenance(pin: GqlStepPin): GqlProvenance {
  const provenance = pin.attempt?.provenance ?? pin.provenance
  if (!provenance) throw new Error(`the recorded step ${pin.stepName} has no provenance to replay`)
  return provenance
}

async function invocationRecord(
  state: Replay, query: string, variables: Record<string, unknown>, signal: AbortSignal, send: Send,
): Promise<unknown> {
  const data = (await send(query, variables, signal)) as { invocation: GqlInvocation | null }
  const invocation = data.invocation
  if (!invocation) return data
  const pathname = variables.pathname as string
  const final = await recordedOverview(state.jobId, pathname, send)
  const level = overviewAt(final, currentPlan(state, final), state.progress, Date.now())
  const byNode = new Map<string, Awaited<ReturnType<typeof runAt>>>()
  for (const nodeId of new Set(invocation.spawns.map((spawn) => spawn.nodeIdentity)))
    byNode.set(nodeId, await runAt(state, pathname, nodeId, send))
  const spawns = invocation.spawns.flatMap((spawn) => {
    const run = byNode.get(spawn.nodeIdentity)!
    const index = run.recorded.indexOfKey.get(keyOf(spawn.key))
    if (index === undefined) throw new Error(`a spawn of ${spawn.nodeIdentity} is not in its run's pages`)
    const now = run.stateOf(index)
    return now === null ? [] : [replayedSpawn(spawn, now)]
  })
  return {
    invocation: { ...invocation, state: level.state, resultCid: level.resultCid, error: level.error, spawns },
  }
}

/**
 * The progress that keeps the level last shown at the same moment when the replay's failure
 * changes to `failAt`: up to a failure the two schedules agree in simulated time, so the same
 * moment is the same time, as a share of a different length.
 */
export function progressAcross(state: Replay, failAt: number | null): number {
  if (lastLevel?.jobId !== state.jobId) return state.progress
  const from = currentPlan(state, lastLevel.final)
  const to = planOf(state.jobId, lastLevel.final, failAt)
  return Math.min(1, timeAt(from, state.progress) / to.makespan)
}

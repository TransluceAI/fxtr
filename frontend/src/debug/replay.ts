/**
 * A recorded level replayed on a made-up clock, for watching how the viewer shows progress
 * without running anything. Development only: the run replay (`RunReplay`, `replayData.ts`) is the
 * one user of this module.
 *
 * The level's overview as recorded is the end of the replay. `schedule` invents a timeline that
 * ends there, the way the runner gets there: every node is in the graph from the start; a run
 * node waits until each run upstream of it (through derived nodes) has finished, then expands
 * into its invocations, which queue and run at most `CONCURRENCY` at a time, first queued first
 * run, in index order. Each invocation takes a pseudo-random duration seeded by its node, so a
 * replay is the same every time, and ends in one of the states the recorded counts give its node
 * (its *recorded* state). `spawnIndices` gives each recorded invocation the index of one with its
 * recorded state, and `stateAt` says what state an index is in at a point of the timeline;
 * `overviewAt` reads the whole level there.
 *
 * **Failure.** A schedule may also fail a run partway (`failAt`): the run with the most
 * invocations running at that point fails some of the invocations that end after it, and the runs
 * downstream of it never expand. Up to that point the timeline is the one without the failure;
 * after it, the level ends failed, and sooner.
 *
 * **Activity.** A replayed run's activity (when it first started and last finished, how many
 * finished, its oldest running invocation) is read off the timeline too, on the wall clock
 * (`replayedTime`), and its failures are the recorded groups scaled to the failed count so far (a
 * replayed failure's own, `ReplayedFailure`, where none were recorded). The overview is read "now"
 * (`asOf`), so the viewer's rate, time left and last finish follow the replay.
 */

import type { GqlFailureGroup, GqlRunActivity, LevelOverview, OverviewNode, StateCounts } from '../api/graphql'

/** How many invocations run at once: the runner's default step limit. */
export const CONCURRENCY = 8

/** The share of the failing run's later invocations that fail (the first of them always does). */
const FAILING_SHARE = 0.3

export type CountedState = keyof StateCounts

const COUNTED_STATES: readonly CountedState[] = [
  'queued', 'running', 'completed', 'failed', 'interrupted', 'conflict', 'stopped',
]

export interface NodeTimeline {
  /** When the node expands into its invocations; `Infinity` for one that never does. */
  ready: number
  /** When each invocation starts, by index; ascending, since invocations start in index order. */
  starts: number[]
  /** When each invocation ends, by index. */
  endOf: number[]
  /** For each state an invocation can end in, when the invocations ending in it end, ascending. */
  ends: Map<CountedState, number[]>
  /** Each invocation's recorded state, by index. */
  recorded: CountedState[]
  /** The state each invocation ends in here, by index: its recorded one, or `failed`. */
  outcomes: CountedState[]
}

export interface Schedule {
  /** When the last invocation ends: the replay's length in simulated time. */
  makespan: number
  /** Each run node's timeline, by identity. */
  runs: Map<string, NodeTimeline>
  /** The run failed partway, when one is. */
  failure: { node: string; name: string } | null
}

const isRun = (node: OverviewNode) => node.counts !== null

/**
 * A timeline ending in the recorded overview `nodes`, or, with `failAt`, one failing a run at that
 * share of the way through the timeline without the failure (see the module docstring).
 */
export function schedule(nodes: readonly OverviewNode[], failAt: number | null = null): Schedule {
  const plain = simulate(nodes, null)
  if (failAt === null) return { ...plain, failure: null }
  const at = failAt * plain.makespan
  const failing = failingRun(nodes, plain.runs, at)
  const timeline = plain.runs.get(failing.identity)!
  const random = seededRandom(`${failing.identity}:failure`)
  const later = timeline.endOf
    .map((end, index) => ({ end, index }))
    .filter(({ end }) => end > at)
    .sort((a, b) => a.end - b.end)
  const fails = new Set(later.filter((_, i) => i === 0 || random() < FAILING_SHARE).map(({ index }) => index))
  if (fails.size === 0) fails.add(timeline.endOf.indexOf(Math.max(...timeline.endOf)))
  const failed = simulate(nodes, { node: failing.identity, fails })
  return { ...failed, failure: { node: failing.identity, name: failing.name ?? failing.identity } }
}

/** The run with the most invocations running at `at`, else the one with the most invocations. */
function failingRun(nodes: readonly OverviewNode[], runs: Map<string, NodeTimeline>, at: number): OverviewNode {
  const candidates = nodes.filter((node) => isRun(node) && runs.get(node.identity)!.recorded.length > 0)
  if (candidates.length === 0) throw new Error('the level has no invocations to fail')
  const runningAt = (node: OverviewNode) =>
    runs.get(node.identity)!.recorded.filter((_, index) => stateAt(runs.get(node.identity)!, index, at) === 'running').length
  const busiest = candidates.reduce((best, node) => (runningAt(node) > runningAt(best) ? node : best))
  if (runningAt(busiest) > 0) return busiest
  return candidates.reduce((best, node) =>
    runs.get(node.identity)!.recorded.length > runs.get(best.identity)!.recorded.length ? node : best)
}

function simulate(
  nodes: readonly OverviewNode[],
  injected: { node: string; fails: Set<number> } | null,
): Omit<Schedule, 'failure'> {
  const byId = new Map(nodes.map((node) => [node.identity, node]))
  const upstream = upstreamRuns(byId)
  const downstream = new Map<string, string[]>()
  const unfinishedInputs = new Map<string, number>()
  const runs = new Map<string, NodeTimeline>()
  const durations = new Map<string, () => number>()
  for (const node of nodes) {
    if (!isRun(node)) continue
    const ups = upstream(node.identity)
    unfinishedInputs.set(node.identity, ups.size)
    for (const up of ups) {
      const after = downstream.get(up)
      if (after) after.push(node.identity)
      else downstream.set(up, [node.identity])
    }
    const random = seededRandom(node.identity)
    const recorded = shuffled(outcomesOf(node.counts!), random)
    const outcomes = injected?.node === node.identity
      ? recorded.map((state, index) => (injected.fails.has(index) ? 'failed' : state))
      : recorded
    runs.set(node.identity, { ready: Infinity, starts: [], endOf: [], ends: new Map(), recorded, outcomes })
    durations.set(node.identity, () => 0.5 + random())
  }

  const queue: { id: string; index: number }[] = []
  let head = 0
  const remaining = new Map<string, number>()
  const finish = (id: string, at: number) => {
    // A failed run gives the runs reading it no value, so they never expand.
    if (id === injected?.node) return
    for (const next of downstream.get(id) ?? []) {
      const left = unfinishedInputs.get(next)! - 1
      unfinishedInputs.set(next, left)
      if (left === 0) expand(next, at)
    }
  }
  const expand = (id: string, at: number) => {
    const total = runs.get(id)!.recorded.length
    runs.get(id)!.ready = at
    remaining.set(id, total)
    if (total === 0) finish(id, at)
    for (let index = 0; index < total; index++) queue.push({ id, index })
  }
  for (const [id, left] of unfinishedInputs) if (left === 0) expand(id, 0)

  const running: { id: string; index: number; end: number }[] = []
  let now = 0
  for (;;) {
    while (running.length < CONCURRENCY && head < queue.length) {
      const { id, index } = queue[head++]
      runs.get(id)!.starts.push(now)
      running.push({ id, index, end: now + durations.get(id)!() })
    }
    if (running.length === 0) break
    let first = 0
    for (let i = 1; i < running.length; i++) if (running[i].end < running[first].end) first = i
    const [{ id, index, end }] = running.splice(first, 1)
    now = end
    const timeline = runs.get(id)!
    timeline.endOf[index] = end
    const state = timeline.outcomes[index]
    const inState = timeline.ends.get(state)
    if (inState) inState.push(end)
    else timeline.ends.set(state, [end])
    const left = remaining.get(id)! - 1
    remaining.set(id, left)
    if (left === 0) finish(id, end)
  }
  for (const timeline of runs.values()) for (const ends of timeline.ends.values()) ends.sort((a, b) => a - b)
  return { makespan: now, runs }
}

/**
 * The state the invocation at `index` is in at `at`, or null before its node expands (it does not
 * exist yet).
 */
export function stateAt(timeline: NodeTimeline, index: number, at: number): CountedState | null {
  if (at < timeline.ready) return null
  if (at < timeline.starts[index]) return 'queued'
  if (at < timeline.endOf[index]) return 'running'
  return timeline.outcomes[index]
}

/**
 * The timeline index of each of a node's recorded invocations, given their recorded states in the
 * backend's order: the invocations recorded in a state take, in order, the indices recorded in it.
 */
export function spawnIndices(timeline: NodeTimeline, states: readonly string[]): number[] {
  const free = new Map<string, number[]>()
  timeline.recorded.forEach((state, index) => {
    const list = free.get(state)
    if (list) list.push(index)
    else free.set(state, [index])
  })
  const taken = new Map<string, number>()
  return states.map((state) => {
    const position = taken.get(state) ?? 0
    const index = free.get(state)?.[position]
    if (index === undefined) throw new Error(`the run has more ${state} invocations than its overview counted`)
    taken.set(state, position + 1)
    return index
  })
}

/** Where `progress` (0 to 1) falls in `plan`'s simulated time. */
export const timeAt = (plan: Schedule, progress: number) => Math.min(1, progress) * plan.makespan

/**
 * The level as it stood at `progress` (0 to 1) through `plan`, a schedule of `final`. Without a
 * failure, at 1 it is `final` itself. Before the end the invocation is running, has no result, and
 * started as long before `now` as the recorded run had been going by then; with a failure it ends
 * failed.
 */
export function overviewAt(final: LevelOverview, plan: Schedule, progress: number, now: number): LevelOverview {
  if (progress >= 1 && plan.failure === null) return final
  const at = timeAt(plan, progress)
  const nodes = final.nodes.map((node) => {
    const timeline = plan.runs.get(node.identity)
    if (!timeline) return node
    const counts = countsAt(timeline, at)
    return {
      ...node,
      counts,
      activity: activityAt(timeline, at, (then) => replayedTime(final, plan, at, then, now)),
      failures: failuresAt(node.failures ?? [], counts.failed),
      version: `${node.version}:replay:${COUNTED_STATES.map((s) => counts[s]).join('.')}`,
    }
  })
  const asOf = new Date(now).toISOString()
  const version = `${final.version}:replay:${plan.failure?.node ?? ''}:${progress}`
  if (progress >= 1)
    return {
      ...final,
      nodes,
      state: 'failed',
      resultCid: null,
      error: { typeName: 'ReplayedFailure', message: `${plan.failure!.name} failed (replayed)` },
      returnedNode: null,
      awaitingNode: null,
      version,
      asOf,
    }
  return {
    ...final,
    nodes,
    state: 'running',
    resultCid: null,
    error: null,
    startedAt: replayedStart(final, progress, now),
    finishedAt: null,
    returnedNode: null,
    awaitingNode: null,
    version,
    asOf,
  }
}

/** A run's activity at `at` in its timeline, with simulated times put on the wall clock by `wall`. */
function activityAt(timeline: NodeTimeline, at: number, wall: (then: number) => string): GqlRunActivity {
  let finished = 0
  let lastFinished = -Infinity
  let oldestRunning = Infinity
  timeline.starts.forEach((start, index) => {
    if (start > at) return
    const end = timeline.endOf[index]
    if (end > at) oldestRunning = Math.min(oldestRunning, start)
    else if (timeline.outcomes[index] === 'completed' || timeline.outcomes[index] === 'failed') {
      finished += 1
      lastFinished = Math.max(lastFinished, end)
    }
  })
  const first = timeline.starts[0]
  return {
    firstStartedAt: first !== undefined && first <= at ? wall(first) : null,
    lastFinishedAt: finished > 0 ? wall(lastFinished) : null,
    finished,
    oldestRunningSince: oldestRunning < Infinity ? wall(oldestRunning) : null,
  }
}

/** The recorded failure groups scaled to `failed` (largest remainders), or one replayed group. */
function failuresAt(recorded: readonly GqlFailureGroup[], failed: number): GqlFailureGroup[] {
  if (failed === 0) return []
  const total = recorded.reduce((sum, group) => sum + group.count, 0)
  if (total === 0) return [{ typeName: 'ReplayedFailure', message: 'failed in the replay', count: failed }]
  const shares = recorded.map((group) => ({ group, exact: (group.count / total) * failed }))
  const counts = shares.map(({ exact }) => Math.floor(exact))
  let left = failed - counts.reduce((sum, n) => sum + n, 0)
  const byRemainder = shares.map((share, index) => ({ index, rest: share.exact - counts[index] })).sort((a, b) => b.rest - a.rest)
  for (const { index } of byRemainder) {
    if (left === 0) break
    counts[index] += 1
    left -= 1
  }
  return shares.map(({ group }, index) => ({ ...group, count: counts[index] })).filter((group) => group.count > 0)
}

/** When something that happened at `then` in `plan` would have, if `at` were `now`, in the
 *  recorded run's time. */
export function replayedTime(final: LevelOverview, plan: Schedule, at: number, then: number, now: number): string {
  const length = final.startedAt && final.finishedAt ? Date.parse(final.finishedAt) - Date.parse(final.startedAt) : 0
  return new Date(now - ((at - then) / (plan.makespan || 1)) * length).toISOString()
}

function countsAt(timeline: NodeTimeline, at: number): StateCounts {
  const counts: StateCounts = { queued: 0, running: 0, completed: 0, failed: 0, interrupted: 0, conflict: 0, stopped: 0 }
  if (at < timeline.ready) return counts
  let ended = 0
  for (const [state, ends] of timeline.ends) {
    const count = countUpTo(ends, at)
    counts[state] += count
    ended += count
  }
  // Every invocation of an expanded node starts at some point, so `starts` counts them all.
  const started = countUpTo(timeline.starts, at)
  counts.running += started - ended
  counts.queued += timeline.starts.length - started
  return counts
}

/** How many of the ascending `times` are at or before `at`. */
function countUpTo(times: readonly number[], at: number): number {
  let low = 0
  let high = times.length
  while (low < high) {
    const mid = (low + high) >> 1
    if (times[mid] <= at) low = mid + 1
    else high = mid
  }
  return low
}

function replayedStart(final: LevelOverview, progress: number, now: number): string | null {
  if (final.startedAt === null || final.finishedAt === null) return final.startedAt
  const length = Date.parse(final.finishedAt) - Date.parse(final.startedAt)
  if (Number.isNaN(length)) throw new Error(`unreadable run times ${final.startedAt} – ${final.finishedAt}`)
  return new Date(now - progress * length).toISOString()
}

/** Each run node's run nodes upstream of it, looking through derived nodes. */
function upstreamRuns(byId: Map<string, OverviewNode>): (id: string) => Set<string> {
  const memo = new Map<string, Set<string>>()
  const through = (id: string): Set<string> => {
    const found = memo.get(id)
    if (found) return found
    const result = new Set<string>()
    memo.set(id, result)
    for (const edge of byId.get(id)?.inputs ?? []) {
      const input = byId.get(edge.identity)
      if (!input) continue
      if (isRun(input)) result.add(input.identity)
      else for (const run of through(input.identity)) result.add(run)
    }
    return result
  }
  return through
}

/** One state per recorded invocation of a node. */
function outcomesOf(counts: StateCounts): CountedState[] {
  return COUNTED_STATES.flatMap((state) => Array<CountedState>(counts[state]).fill(state))
}

function shuffled<T>(items: T[], random: () => number): T[] {
  for (let i = items.length - 1; i > 0; i--) {
    const j = Math.floor(random() * (i + 1))
    ;[items[i], items[j]] = [items[j], items[i]]
  }
  return items
}

/** A pseudo-random sequence in [0, 1) seeded by `seed` (mulberry32 over an FNV-1a hash). */
function seededRandom(seed: string): () => number {
  let state = 0x811c9dc5
  for (let i = 0; i < seed.length; i++) state = Math.imul(state ^ seed.charCodeAt(i), 0x01000193)
  return () => {
    state = (state + 0x6d2b79f5) | 0
    let x = Math.imul(state ^ (state >>> 15), 1 | state)
    x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296
  }
}

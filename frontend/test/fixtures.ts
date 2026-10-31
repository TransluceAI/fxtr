import * as dagCbor from '@ipld/dag-cbor'
import type { GqlInvocation, GqlNode, GqlSpawn, LevelOverview } from '../src/api/graphql'
import type { LevelState } from '../src/lib/levelState'
import type { EntityOpener } from '../src/lib/openEntity'
import type { LinkTarget } from '../src/panes/drag'
import { TRACE } from '../src/panes/columns'
import { formatRel } from '../src/panes/address'
import { Store } from '../src/lib/store'

export const STR_ARRAY = { dims: [], schema: 'str' } as const

export function base64Cbor(value: unknown): string {
  return Buffer.from(dagCbor.encode(value)).toString('base64')
}

export function node(identity: string, kind: string, extra: Partial<GqlNode> = {}): GqlNode {
  return { identity, kind, name: identity, type: { dims: [], schema: 'str' }, params: {}, inputs: [], valueCid: null, ...extra }
}

export function spawn(nodeIdentity: string, key: (string | number)[], state: string, resultCid: string | null): GqlSpawn {
  return { nodeIdentity, key, childPathname: null, stepCacheKey: `k/${key.join('/')}`, resultCid, state }
}

/**
 * A root invocation in progress: an input, a step mapped over two seeds (one done, one running),
 * and a second step whose input is the first's result, not expanded yet. The body's running
 * attempt last asked to observe the first step.
 */
export function inProgress(overrides: Partial<GqlInvocation> = {}): GqlInvocation {
  return {
    pathname: 'root',
    workflowName: 'sweep',
    state: 'running',
    resultCid: null,
    error: null,
    provenance: null,
    nodes: [
      node('in', 'input', { params: { name: 'seeds' }, valueCid: 'cid-in', type: { dims: [['seed', 'str']], schema: 'str' } }),
      node('roll', 'run_step', {
        params: { step_name: 'roll', map_over: ['seed'], replicas: {} },
        inputs: [{ label: 'seed', identity: 'in' }],
        type: { dims: [['seed', 'str']], schema: 'str' },
      }),
      node('grade', 'run_step', {
        params: { step_name: 'grade', map_over: [], replicas: {} },
        inputs: [{ label: 'x', identity: 'roll' }],
      }),
    ],
    spawns: [spawn('roll', ['a'], 'completed', 'cid-a'), spawn('roll', ['b'], 'running', null)],
    attempts: [
      {
        kind: 'workflow',
        state: 'running',
        startedAt: '2026-09-22T10:00:01Z',
        finishedAt: null,
        error: null,
        provenance: { jobId: 'job', commit: 'c0ffee', ephemeral: false },
        events: [
          { kind: 'emit', identity: 'roll' },
          { kind: 'observe', identity: 'roll' },
        ],
      },
    ],
    ...overrides,
  }
}

/** The graph summary of a full fixture; contents remain available only through page fixtures. */
export function overviewOf(invocation: GqlInvocation = inProgress(), version = 'v1'): LevelOverview {
  const latest = invocation.attempts.at(-1)
  return {
    pathname: invocation.pathname, workflowName: invocation.workflowName, state: invocation.state,
    error: invocation.error, resultCid: invocation.resultCid, version, asOf: '2026-10-01T12:00:00Z',
    startedAt: latest?.startedAt ?? null, finishedAt: latest?.finishedAt ?? null,
    attemptCount: invocation.attempts.length,
    returnedNode: latest?.events.find(event => event.kind === 'return')?.identity ?? null,
    awaitingNode: latest?.events.at(-1)?.kind === 'observe' ? latest.events.at(-1)!.identity : null,
    nodes: invocation.nodes.map(node => {
      const {map_over, replicas, ...params} = node.params
      const counts = {queued: 0, running: 0, completed: 0, failed: 0, interrupted: 0, conflict: 0, stopped: 0}
      for (const spawn of invocation.spawns.filter(spawn => spawn.nodeIdentity === node.identity)) {
        counts[spawn.state as keyof typeof counts] += 1
      }
      return {...node, params, version: `${version}/${node.identity}`, mapOver: map_over as string[] ?? [],
        replicaDims: Object.keys(replicas as object ?? {}),
        ...(['run_step', 'run_child_workflow'].includes(node.kind)
          ? {counts, activity: {firstStartedAt: null, lastFinishedAt: null, finished: 0, oldestRunningSince: null}, failures: []}
          : {counts: null, activity: null, failures: null})}
    }),
  }
}

/** A level's shared state with nothing picked, set or stored, and setters that do nothing. */
export function levelState(overrides: Partial<LevelState> = {}): LevelState {
  return { slice: {}, setSlice: () => {}, selected: {}, levelPath: [],
    cardTables: new Store(new Map()), ...overrides }
}

/** An opener of the trace's links that records what each click follows, with one panel the
 *  trace's clicks navigate: a link is lit while what it names (its address and params) is the last
 *  one followed. */
export function recordingOpener(): { opener: EntityOpener; follows: LinkTarget[] } {
  const follows: LinkTarget[] = []
  const shown = (target: LinkTarget) => JSON.stringify([target.rel, target.params])
  const linkState = (target: LinkTarget) => {
    const open = follows.length > 0 && shown(follows.at(-1)!) === shown(target)
    return { selected: open, open }
  }
  return { follows, opener: { originId: TRACE, viewId: null, relative: false, follow: (target) => { follows.push(target) }, hover: () => {}, linkState } }
}

/** A followed link written as its address and params, for asserting on. */
export function followed(target: LinkTarget | undefined): string {
  if (!target) return 'nothing'
  return formatRel(target.rel, target.params)
}

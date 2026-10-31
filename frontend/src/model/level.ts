/**
 * From one workflow invocation, as the backend records it, to the step cards the UI shows.
 *
 * The DAG has three sorts of node. Leaves (`input`, `source_array`) and runs
 * (`run_step`, `run_child_workflow`) become cards. Derived nodes (`combine`, `stack`, `query`)
 * have no stored value and are not cards; they are folded into the argument expressions of the
 * runs that read them, so a query over `reports` shows as `query(` and a chip for `reports`.
 *
 * Cards contain graph metadata and query references. Contents load through independent pages.
 */

import { isStruct } from '../api/cbor'
import { type LevelOverview, type GqlNode, type GqlRunActivity, type GqlSpawn } from '../api/graphql'
import type {
  ArrayTypeJson,
  CardProgress,
  Cell,
  ExprToken,
  KeyValue,
  Level,
  LevelResult,
  NodeKind,
  RunActivity,
  SchemaJson,
  StepArg,
  StepDoc,
  StepInvocation,
  TableRow,
} from '../types'
import { aggregate, countStates, glyphOf, isLive, runStateOf, type Counts, type Glyph, type RunState } from '../lib/progress'
import { columnsFor, keyColumnKey, structFields, valueColumnKey } from './schema'

const LEAF_KINDS = new Set(['input', 'source_array'])
export const RUN_KINDS = new Set(['run_step', 'run_child_workflow'])

/** The id of the table holding a returned value that no card holds (a derived node was returned). */
export const RETURN_ID = 'return'

export function isCardNode(node: GqlNode): boolean {
  return LEAF_KINDS.has(node.kind) || RUN_KINDS.has(node.kind)
}

/**
 * `receivedAt` is this browser's time (`Date.now()`) when the overview arrived: with its `asOf`, how
 * far the database's clock is ahead, which `RunActivity` times are shifted back by. By default the
 * two clocks are taken to agree.
 */
export function buildLevel(
  jobId: string,
  invocation: LevelOverview,
  receivedAt: number = Date.parse(invocation.asOf),
): Level {
  const asOf = Date.parse(invocation.asOf)
  if (Number.isNaN(asOf)) throw new Error(`the overview of ${invocation.pathname} has no asOf time`)
  const serverAhead = asOf - receivedAt
  const nodes = new Map(invocation.nodes.map((node) => [node.identity, node]))
  const returnedId = invocation.returnedNode
  const ownState = runStateOf(invocation.state)

  const exprOf = (identity: string): ExprToken[] => {
    const node = nodes.get(identity)
    if (!node) return [`<${identity.slice(0, 8)}…>`]
    if (isCardNode(node)) return [{ ref: identity }]
    const lit = (value: unknown) => JSON.stringify(value)
    const params = node.params
    switch (node.kind) {
      case 'combine': {
        const entries = node.inputs.flatMap((edge, i): ExprToken[] => [
          i === 0 ? '' : ', ',
          `${String(edge.label)}: `,
          ...exprOf(edge.identity),
        ])
        const join = params.join && params.join !== 'exact' ? `, join=${lit(params.join)}` : ''
        return ['combine({', ...entries, `}${join})`]
      }
      case 'stack': {
        const entries = node.inputs.flatMap((edge, i): ExprToken[] => [
          i === 0 ? '' : ', ',
          `${lit(edge.label)}: `,
          ...exprOf(edge.identity),
        ])
        return ['stack({', ...entries, `}, dim=${lit(params.dim)})`]
      }
      case 'query': {
        // A query node's plan is stored encoded, so only the arrays it reads are shown, in the
        // order the plan reads them (each edge's label is that position).
        const ordered = [...node.inputs].sort((a, b) => Number(a.label) - Number(b.label))
        const entries = ordered.flatMap((edge, i): ExprToken[] => [
          i === 0 ? '' : ', ',
          ...exprOf(edge.identity),
        ])
        return ['query(', ...entries, ')']
      }
      default:
        return [node.kind]
    }
  }

  const steps: StepDoc[] = []
  for (const node of invocation.nodes) {
    if (!isCardNode(node)) continue
    const isRun = RUN_KINDS.has(node.kind)
    const params = node.params
    const kind: NodeKind =
      node.kind === 'run_child_workflow'
        ? 'workflow'
        : node.kind === 'run_step'
          ? 'step'
          : 'source'
    const mapOver = node.mapOver
    const replicaDims = node.replicaDims
    const args: StepArg[] = node.inputs.map((edge) => ({
      name: String(edge.label),
      expr: exprOf(edge.identity),
      dims: (nodes.get(edge.identity)?.type.dims ?? []).map(([name]) => name),
    }))
    const progress: CardProgress =
      node.counts !== null
        ? { ...progressFromCounts(node.counts, ownState), activity: activityOf(node.activity, serverAhead), failures: node.failures ?? [] }
        : LEAF_PROGRESS
    steps.push({
      id: node.identity,
      name: node.name ?? (typeof params.name === 'string' ? params.name : node.kind),
      kind,
      method: methodOf(node),
      codeVersion: invocation.codeVersion,
      type: node.type,
      dims: node.type.dims.map(([name]) => name),
      mapOver,
      replicaDims,
      step: steps.length + 1,
      deps: depsOf(args, node.identity),
      args,
      columns: columnsFor(node.type),
      progress,
      output: isRun ? { jobId, pathname: invocation.pathname, nodeId: node.identity }
        : node.valueCid ? { cid: node.valueCid } : null,
      browse: isRun ? { jobId, pathname: invocation.pathname, nodeId: node.identity } : null,
      version: node.version,
    })
  }

  let result: LevelResult | null = null
  if (returnedId !== null) {
    const returnedStep = steps.find((step) => step.id === returnedId) ?? null
    let table: StepDoc | null = null
    // A workflow that returns a derived node has a result no card holds; it gets a table of its own.
    if (!returnedStep && invocation.resultCid) {
      const type = nodes.get(returnedId)?.type
      if (type) {
        table = {
          id: RETURN_ID,
          name: 'return',
          kind: 'source',
          method: null,
          codeVersion: invocation.codeVersion,
          type,
          dims: type.dims.map(([name]) => name),
          mapOver: [],
          replicaDims: [],
          step: steps.length + 1,
          deps: [],
          args: [],
          columns: columnsFor(type),
          progress: LEAF_PROGRESS,
          output: { cid: invocation.resultCid },
          browse: null,
          version: invocation.version,
        }
      }
    }
    result = { stepId: returnedStep?.id ?? null, expr: exprOf(returnedId), table }
  }

  return {
    jobId,
    pathname: invocation.pathname,
    workflowName: invocation.workflowName,
    codeVersion: invocation.codeVersion,
    status: invocation,
    attemptCount: invocation.attemptCount,
    steps,
    result,
    resultCid: invocation.resultCid,
    glyph: invocationGlyph(invocation),
    live: isOverviewLive(invocation),
    awaiting: invocation.awaitingNode ? { id: invocation.awaitingNode, expr: exprOf(invocation.awaitingNode) } : null,
  }
}

/** The progress of a node that was registered rather than run. */
export const LEAF_PROGRESS: CardProgress = { glyph: glyphOf('done'), counts: null, activity: null, failures: [] }

/** A run's activity with its times on this browser's clock; a run node always has one. */
function activityOf(activity: GqlRunActivity | null, serverAhead: number): RunActivity {
  if (activity === null) throw new Error('a run node came without its activity')
  const time = (iso: string | null) => (iso === null ? null : Date.parse(iso) - serverAhead)
  return {
    firstStartedAt: time(activity.firstStartedAt),
    lastFinishedAt: time(activity.lastFinishedAt),
    finished: activity.finished,
    oldestRunningSince: time(activity.oldestRunningSince),
  }
}

export function progressFromCounts(raw: Record<string, number>, ownState: RunState = 'done'): { counts: Counts; glyph: Glyph } {
  const counts = countStates([])
  for (const [state, count] of Object.entries(raw)) {
    const name = runStateOf(state)
    counts.total += count
    if (name !== 'waiting') counts[name] += count
  }
  const states = Object.entries(raw).filter(([, n]) => n > 0).map(([state]) => runStateOf(state))
  return { counts, glyph: states.length ? aggregate(states) : glyphOf(isLive(ownState) ? 'waiting' : 'stopped') }
}

/** A workflow's own glyph, red when any of its runs has a failed target. */
export function invocationGlyph(invocation: LevelOverview): Glyph {
  const own = runStateOf(invocation.state)
  return { shape: own, failed: own === 'failed' || invocation.nodes.some(node => (node.counts?.failed ?? 0) > 0) }
}

export function isOverviewLive(invocation: LevelOverview): boolean {
  return isLive(runStateOf(invocation.state)) || invocation.nodes.some(node =>
    (node.counts?.running ?? 0) + (node.counts?.queued ?? 0) > 0)
}

/** The registered step / workflow name a run node calls; null for a node with no function. */
export function methodOf(node: GqlNode): string | null {
  const { step_name, workflow_name } = node.params
  if (typeof step_name === 'string') return step_name
  if (typeof workflow_name === 'string') return workflow_name
  return null
}

function depsOf(args: StepArg[], self: string): string[] {
  const deps: string[] = []
  for (const arg of args)
    for (const token of arg.expr)
      if (typeof token !== 'string' && token.ref !== self && !deps.includes(token.ref)) deps.push(token.ref)
  return deps
}

export function asStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : []
}

export function asReplicas(value: unknown): Record<string, KeyValue[]> {
  if (typeof value !== 'object' || value === null) return {}
  const out: Record<string, KeyValue[]> = {}
  for (const [dim, values] of Object.entries(value as Record<string, unknown>))
    if (Array.isArray(values)) out[dim] = values.filter(isKeyValue)
  return out
}

const isKeyValue = (value: unknown): value is KeyValue =>
  typeof value === 'string' || typeof value === 'number'

export function invocationOf(spawn: GqlSpawn, dims: string[]): StepInvocation {
  const key: Record<string, KeyValue> = {}
  dims.forEach((dim, index) => {
    if (spawn.key[index] !== undefined) key[dim] = spawn.key[index]
  })
  return {
    key,
    childPathname: spawn.childPathname,
    stepCacheKey: spawn.stepCacheKey,
    resultCid: spawn.resultCid,
    state: runStateOf(spawn.state),
  }
}

export function makeRow(id: string, type: ArrayTypeJson, key: KeyValue[], value: Cell): TableRow {
  const cells: Record<string, Cell> = {}
  type.dims.forEach(([name], i) => {
    cells[keyColumnKey(name)] = key[i]
  })
  return { id: `${id}#${JSON.stringify(key)}`, key, cells: { ...cells, ...valueCells(type.schema, value) } }
}

/** A value's cells by value column: a struct's fields, or the one anonymous cell. */
export function valueCells(schema: SchemaJson, value: Cell): Record<string, Cell> {
  const fields = structFields(schema)
  // A nullable struct that is null still has the struct's columns, all empty.
  if (fields) return Object.fromEntries(Object.keys(fields).map((name) => [valueColumnKey(name), isStruct(value) ? (value[name] ?? null) : null]))
  return { [valueColumnKey(null)]: value }
}

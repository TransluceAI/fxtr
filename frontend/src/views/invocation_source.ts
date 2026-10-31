/**
 * Invocation records for the view system, built from what the backend records and kept current
 * while a job runs: `useInvocationRecord` is the shell's `useInvocation`, reading the live store
 * (`api/live.ts`), so a renderer showing an invocation in progress re-renders as it moves. A
 * workflow invocation's record comes from the same invocation entry the level on screen uses; a
 * step's from its pin plus the `StepRequest` entity behind `requestCid`, whose `inputs` map is the
 * sliced input arrays of that one spawn by argument name. Every array is handed on as an
 * `EntityID`, for the renderer to load lazily through `useEntity`.
 */

import { useMemo, useSyncExternalStore } from 'react'
import {
  EntityID,
  isDataMap,
  isEntityID,
  invocationRefKey,
  type ArrayData,
  type ChildRun,
  type EntityData,
  type InvocationRecord,
  type InvocationRef,
  type LoadResult,
  type Spawn,
} from 'fxtr-view'
import { invocationStatus, type GqlInvocation, type GqlSpawn, type GqlStepPin } from '../api/graphql'
import { invocationEntry, stepEntry, useEntryState, useWatchEntry } from '../api/live'
import { RUN_KINDS, asReplicas, asStrings, methodOf } from '../model/level'
import { loadEntity } from './entity_loader'

const STEP_REQUEST_TYPE = 'org.transluce.fxtr.runner.StepRequest'

/** The shell's `useInvocation`: the record behind a ref, current while the caller is mounted. */
export function useInvocationRecord(ref: InvocationRef): LoadResult<InvocationRecord> {
  const refKey = invocationRefKey(ref)
  const workflow = ref.kind === 'workflow' ? invocationEntry(ref.jobId, ref.pathname) : null
  const step = ref.kind === 'step' ? stepEntry(ref.jobId, ref.cacheKey) : null
  useWatchEntry(workflow ?? step)
  const workflowState = useEntryState(workflow)
  const stepState = useEntryState(step)
  const state = workflowState ?? stepState
  if (state === null) throw new Error(`no entry for invocation ref ${JSON.stringify(ref)}`)
  const value = state.status === 'ready' ? state.value : null
  const requestCid = step && value ? (value as GqlStepPin).requestCid : null
  const request = useStepRequest(requestCid)
  const record = useMemo((): LoadResult<InvocationRecord> | null => {
    if (value === null) return null
    if (ref.kind === 'workflow') {
      const invocation = (value as { invocation: GqlInvocation }).invocation
      return { status: 'loaded', value: workflowRecord(ref, invocation) }
    }
    if (request === null) throw new Error('a step record needs its request')
    if (request.status !== 'loaded') return request
    return { status: 'loaded', value: stepRecord(ref, value as GqlStepPin, request.value) }
  }, [refKey, value, request])
  if (state.status === 'pending') return { status: 'loading', promise: state.promise }
  if (state.status === 'error') return { status: 'error', error: state.error }
  if (record === null) throw new Error('a ready entry builds a record')
  return record
}

/** Step request entities by cid: content-addressed, so each is loaded once for the page. */
const requests = new Map<string, LoadResult<EntityData>>()
const requestListeners = new Set<() => void>()

/** The step request entity at a cid (null for none), loaded once and shared. */
function useStepRequest(cid: string | null): LoadResult<EntityData> | null {
  const result = useSyncExternalStore(
    (listener) => {
      requestListeners.add(listener)
      return () => requestListeners.delete(listener)
    },
    () => (cid === null ? null : (requests.get(cid) ?? null)),
  )
  if (cid === null) return null
  if (result !== null) return result
  const promise = loadEntity(new EntityID(cid)).then(
    (value) => settleRequest(cid, { status: 'loaded', value }),
    (error: unknown) => settleRequest(cid, { status: 'error', error }),
  )
  const loading: LoadResult<EntityData> = { status: 'loading', promise }
  requests.set(cid, loading)
  return loading
}

function settleRequest(cid: string, result: LoadResult<EntityData>) {
  requests.set(cid, result)
  for (const listener of [...requestListeners]) listener()
}

function arrayId(cid: string): EntityID<ArrayData> {
  return new EntityID(cid) as EntityID<ArrayData>
}

function workflowRecord(ref: InvocationRef & { kind: 'workflow' }, invocation: GqlInvocation): InvocationRecord {
  const inputs: Record<string, EntityID<ArrayData>> = {}
  for (const node of invocation.nodes) {
    if (node.kind !== 'input') continue
    const name = node.params.name
    if (typeof name !== 'string' || node.valueCid === null)
      throw new Error(`input node ${node.identity} of ${ref.pathname} has no name or no value`)
    inputs[name] = arrayId(node.valueCid)
  }
  const spawnsByNode = new Map<string, GqlSpawn[]>()
  for (const spawn of invocation.spawns) {
    const list = spawnsByNode.get(spawn.nodeIdentity) ?? []
    list.push(spawn)
    spawnsByNode.set(spawn.nodeIdentity, list)
  }
  const children: ChildRun[] = []
  for (const node of invocation.nodes) {
    if (!RUN_KINDS.has(node.kind)) continue
    const name = methodOf(node)
    if (name === null || node.name === null) throw new Error(`run node ${node.identity} of ${ref.pathname} names no function`)
    const dims = [...asStrings(node.params.map_over), ...Object.keys(asReplicas(node.params.replicas))]
    children.push({
      nodeName: node.name,
      name,
      kind: node.kind === 'run_child_workflow' ? 'workflow' : 'step',
      dims,
      spawns: (spawnsByNode.get(node.identity) ?? []).map((spawn) => childSpawn(ref.jobId, node.identity, dims, spawn)),
    })
  }
  const status = invocationStatus(invocation)
  return {
    ref,
    name: invocation.workflowName,
    state: status.state,
    error: status.error,
    inputs,
    output: invocation.resultCid ? arrayId(invocation.resultCid) : null,
    children,
  }
}

/** A spawn from its GraphQL row; the positional key is named by the node's dims. */
function childSpawn(jobId: string, nodeIdentity: string, dims: readonly string[], spawn: GqlSpawn): Spawn {
  const ref: InvocationRef | null = spawn.childPathname
    ? { jobId, kind: 'workflow', pathname: spawn.childPathname }
    : spawn.stepCacheKey
      ? { jobId, kind: 'step', cacheKey: spawn.stepCacheKey }
      : null
  if (!ref) throw new Error(`spawn ${JSON.stringify(spawn.key)} of node ${nodeIdentity} names neither a child nor a step`)
  if (spawn.key.length !== dims.length)
    throw new Error(`spawn ${JSON.stringify(spawn.key)} of node ${nodeIdentity} has ${spawn.key.length} key values for ${dims.length} dims`)
  return { key: Object.fromEntries(dims.map((dim, i) => [dim, spawn.key[i]])), ref, output: spawn.resultCid ? arrayId(spawn.resultCid) : null }
}

function stepRecord(ref: InvocationRef & { kind: 'step' }, pin: GqlStepPin, request: EntityData): InvocationRecord {
  if (request.$type !== STEP_REQUEST_TYPE) throw new Error(`${pin.requestCid} is a ${String(request.$type)}, not a step request`)
  const rawInputs = request['inputs']
  if (!isDataMap(rawInputs)) throw new Error(`step request ${pin.requestCid} has no inputs map`)
  const inputs: Record<string, EntityID<ArrayData>> = {}
  for (const [name, id] of Object.entries(rawInputs)) {
    if (!isEntityID(id)) throw new Error(`input ${name} of step request ${pin.requestCid} is not an entity id`)
    inputs[name] = id as EntityID<ArrayData>
  }
  return {
    ref,
    name: pin.stepName,
    state: pin.state,
    error: pin.attempt?.error ?? null,
    inputs,
    output: pin.resultCid ? arrayId(pin.resultCid) : null,
    children: [],
  }
}

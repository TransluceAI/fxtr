import { EntityID, type ArrayData, type InvocationRef, type Spawn } from 'fxtr-view'
import type { Column, KeyValue, TableDescriptor, StepInvocation } from '../types'

/**
 * How a dim relates to the step's invocations: `mapped` dims fan the body out, `replica` dims
 * are fxtr's native repeats, and every other dim is produced inside one invocation.
 */
export type DimKind = 'mapped' | 'replica' | 'other'

export function dimKind(step: TableDescriptor, dim: string): DimKind {
  if (step.mapOver.includes(dim)) return 'mapped'
  if (step.replicaDims.includes(dim)) return 'replica'
  return 'other'
}

/** The key columns that together identify one invocation of the step, in dim order. */
export function invocationKeyColumns(step: TableDescriptor): Column[] {
  return step.columns.filter(
    (column) => column.group === 'key' && dimKind(step, column.label) !== 'other',
  )
}

/** The dims that identify one invocation of the step. */
export function invocationKeyDims(step: TableDescriptor): string[] {
  return invocationKeyColumns(step).map((column) => column.label)
}

/** Whether the step fans out over invocation dims, so its card shows keys rather than its one
 *  invocation's output. */
export function isMapped(step: TableDescriptor): boolean {
  return invocationKeyDims(step).length > 0
}

/** The level's slice restricted to the step's invocation dims: the invocations it picks out. */
export function invocationSliceFor(step: TableDescriptor, levelSlice: Readonly<Record<string, KeyValue>>): Record<string, KeyValue> {
  return pick(levelSlice, invocationKeyDims(step))
}

/** The level's slice restricted to every dim of the step's output: the rows it picks out,
 *  slicing on dims produced inside an invocation as well as on invocation dims. */
export function rowSliceFor(step: TableDescriptor, levelSlice: Readonly<Record<string, KeyValue>>): Record<string, KeyValue> {
  return pick(levelSlice, step.type.dims.map(([dim]) => dim))
}

function pick(levelSlice: Readonly<Record<string, KeyValue>>, dims: readonly string[]): Record<string, KeyValue> {
  const slice: Record<string, KeyValue> = {}
  for (const dim of dims) {
    if (Object.hasOwn(levelSlice, dim)) slice[dim] = levelSlice[dim]
  }
  return slice
}

/** The view-system ref of a run node's invocation: its child workflow or its step; null for one
 *  that was never scheduled. */
export function invocationRef(jobId: string, invocation: StepInvocation): InvocationRef | null {
  if (invocation.childPathname) return { jobId, kind: 'workflow', pathname: invocation.childPathname }
  if (invocation.stepCacheKey) return { jobId, kind: 'step', cacheKey: invocation.stepCacheKey }
  return null
}

/** A run node's invocation as the view system's spawn; null for one that was never scheduled. */
export function spawnOf(jobId: string, invocation: StepInvocation): Spawn | null {
  const ref = invocationRef(jobId, invocation)
  if (!ref) return null
  return { key: invocation.key, ref, output: invocation.resultCid ? (new EntityID(invocation.resultCid) as EntityID<ArrayData>) : null }
}

/** The dims of a step's invocation key, mapped dims then replica dims. */
export function invocationDims(step: TableDescriptor): string[] {
  return [...step.mapOver, ...step.replicaDims]
}

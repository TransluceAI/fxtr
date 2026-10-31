import { fetchKeyPage, fetchOutputDimValues, useQuery } from '../api/browse'
import type { KeyValue, RunSource, StepDoc } from '../types'
import { dimKind, rowSliceFor, invocationSliceFor } from './invocations'
import { outputSourceFor } from './output'

/** One value of a dim, and whether the other picks leave it possible. */
export interface KeyListItem { value: KeyValue; compatible: boolean }
/** The first `KEY_LIST_LIMIT` values matching a search; `hasMore` when others match too. */
export interface KeyList { items: KeyListItem[]; hasMore: boolean }

/** How many of a dim's values a list reads (the server's `KEY_LIST_LIMIT`); a search finds the rest. */
export const KEY_LIST_LIMIT = 1000

/**
 * Where a dim's values are read for the level's slice panel: a run the dim keys the invocations of
 * (`invocations`: the spawn table's keys, cheap), or else the first node whose output has the dim
 * (`output`: read from its arrays, since no table records a dim produced inside an invocation).
 */
export type DimSource = { kind: 'invocations'; step: StepDoc } | { kind: 'output'; step: StepDoc }

/** A dim of the level, and where its values are read (null while no node has output with it). */
export interface LevelDim { dim: string; source: DimSource | null }

/** A dim of one node, read from that node: from its invocations when the dim keys them, else from
 *  its output. */
export function nodeDim(step: StepDoc, dim: string): LevelDim {
  const keyed = step.browse !== null && dimKind(step, dim) !== 'other'
  return { dim, source: { kind: keyed ? 'invocations' : 'output', step } }
}

/** Every dim of the level's nodes, in order of first appearance, and where its values are read. */
export function levelDims(steps: readonly StepDoc[]): LevelDim[] {
  const dims: string[] = []
  for (const step of steps) {
    for (const [dim] of step.type.dims) if (!dims.includes(dim)) dims.push(dim)
  }
  return dims.map((dim) => {
    const keyed = steps.find((step) => step.browse !== null && step.type.dims.some(([name]) => name === dim) && dimKind(step, dim) !== 'other')
    if (keyed) return { dim, source: { kind: 'invocations', step: keyed } }
    const produced = steps.find((step) => step.output !== null && step.type.dims.some(([name]) => name === dim))
    return { dim, source: produced ? { kind: 'output', step: produced } : null }
  })
}

/** The other picks a dim's values are judged compatible against, as its list's `against` key. */
export function againstKey(others: Readonly<Record<string, KeyValue>>): string {
  return JSON.stringify(Object.entries(others).sort(([a], [b]) => a.localeCompare(b)))
}

/** The slice less `dim`: every other dim it is on. */
export function withoutDim(slice: Readonly<Record<string, KeyValue>>, dim: string): Record<string, KeyValue> {
  const { [dim]: _dropped, ...rest } = slice
  return rest
}

/** A loader of a run's key list for one of its invocation dims, with the state of the invocations
 *  each value picks out. */
export function invocationKeyLoader(source: RunSource, dim: string, others: Record<string, KeyValue>) {
  return async (search: string, signal: AbortSignal): Promise<KeyList> => {
    const page = await fetchKeyPage(source, dim, others, search, KEY_LIST_LIMIT, signal)
    return {
      items: page.items.map((item) => ({ value: item.value, compatible: item.compatible })),
      hasMore: !!page.next,
    }
  }
}

/** A dim's values as a `KeyValueList` lists them (in the slice panel, or under a card's table
 *  header): its source's values, compatible against the level's other picks that apply to that
 *  node; null for a dim with no source yet. */
export function levelDimList(entry: LevelDim, slice: Readonly<Record<string, KeyValue>>) {
  const source = entry.source
  if (!source) return null
  const { step } = source
  if (source.kind === 'invocations') {
    const others = withoutDim(invocationSliceFor(step, slice), entry.dim)
    return {
      scope: JSON.stringify(['keys', step.browse, entry.dim]),
      against: againstKey(others),
      version: step.version,
      load: invocationKeyLoader(step.browse!, entry.dim, others),
    }
  }
  const others = withoutDim(rowSliceFor(step, slice), entry.dim)
  const output = outputSourceFor(step, others)!
  return {
    scope: JSON.stringify(['values', step.output, entry.dim]),
    against: againstKey(others),
    version: 'jobId' in output ? step.version : undefined,
    load: async (search: string, signal: AbortSignal): Promise<KeyList> => {
      const page = await fetchOutputDimValues(output, entry.dim, search, KEY_LIST_LIMIT, signal)
      return {
        items: page.items,
        hasMore: page.items.length < page.total,
      }
    },
  }
}

/** How many values a dim of the node has, whatever the slice (null while loading): from its spawn
 *  table for a dim keying its invocations, else from its output. */
export function useDimCount(step: StepDoc, dim: string): number | null {
  const keyed = nodeDim(step, dim).source?.kind === 'invocations'
  const count = useQuery(JSON.stringify(['key-count', keyed ? step.browse : step.output, dim]),
    (signal) => keyed
      ? fetchKeyPage(step.browse!, dim, {}, '', 1, signal)
      : fetchOutputDimValues(outputSourceFor(step, {})!, dim, '', 1, signal),
    { version: keyed || (step.output !== null && 'jobId' in step.output) ? step.version : undefined })
  return count.value?.total ?? null
}

/** Cancellable, component-scoped queries. Pages replace one another instead of accumulating. */
import type { ArrayTypeJson, KeyValue, OutputSource, RunSource, Sort } from '../types'
import { gql, SPAWN_FIELDS, COUNT_FIELDS, type GqlArrayRow, type GqlSpawn, type CountedPage, type StateCounts } from './graphql'
import { useQuery } from './useQuery'
export { useQuery } from './useQuery'

/** Which invocations a run page lists besides the selection: those in one state, or every one that
 *  has not completed. */
export type RunFilter = { state: string } | { unfinished: true } | null

/** A page of the run's invocations the selection picks out, in key order: the first, the ones
 *  after the cursor `after`, or the page holding row `offset` (the last page for one past the end,
 *  `LAST_OFFSET` for the last page whatever its length). */
export function fetchRunPage(source: RunSource, selection: Record<string, KeyValue>, after: string | null = null,
                             first = 25, filter: RunFilter = null, signal?: AbortSignal, offset?: number) {
  const state = filter && 'state' in filter ? filter.state : null
  const unfinished = !!filter && 'unfinished' in filter
  return gql<{ runPage: RunPage }>(RUN_PAGE,
    { ...source, selection, after, first, state, unfinished, ...(offset !== undefined && { offset }) }, signal)
    .then((data) => data.runPage)
}

/** An offset past the end of any run (the largest GraphQL `Int`), which asks for its last page. */
export const LAST_OFFSET = 2 ** 31 - 1

export interface RunPage extends CountedPage<GqlSpawn> {
  /** The first row's offset, for a page asked for by offset. */
  offset?: number | null
}

export const RUN_PAGE = `query($jobId: ID!, $pathname: String!, $nodeId: String!,
  $selection: JSON!, $after: String, $first: Int!, $state: String, $unfinished: Boolean!, $offset: Int) {
  runPage(jobId: $jobId, pathname: $pathname, nodeId: $nodeId, selection: $selection,
    after: $after, first: $first, state: $state, unfinished: $unfinished, offset: $offset) { items { ${SPAWN_FIELDS} } total offset next }
}`

export interface KeyItem { value: KeyValue; compatible: boolean; counts: StateCounts }
/** The first `first` values of a dim, with `next` set when there are more. */
export function fetchKeyPage(source: RunSource, dim: string, selection: Record<string, KeyValue>, search: string,
                             first: number, signal?: AbortSignal) {
  return gql<{ keyPage: CountedPage<KeyItem> }>(`query($jobId: ID!, $pathname: String!, $nodeId: String!,
    $dim: String!, $selection: JSON!, $search: String!, $first: Int!) {
    keyPage(jobId: $jobId, pathname: $pathname, nodeId: $nodeId, dim: $dim,
      selection: $selection, search: $search, first: $first) { items { value compatible counts { ${COUNT_FIELDS} } } total next }
  }`, { ...source, dim, selection, search, first }, signal).then((data) => data.keyPage)
}

export interface DimValue { value: KeyValue; compatible: boolean }
/** The first `first` distinct keys a dim takes in a source's output, read from its arrays (for a dim
 *  no spawn key records); compatible against the source's selection of the other dims. */
export function fetchOutputDimValues(source: OutputSource, dim: string, search: string, first: number, signal?: AbortSignal) {
  return gql<{ outputDimValues: { items: DimValue[]; total: number } }>(`query($source: OutputSource!,
    $dim: String!, $search: String!, $first: Int!) {
    outputDimValues(source: $source, dim: $dim, search: $search, first: $first) { items { value compatible } total }
  }`, { source, dim, search, first }, signal).then((data) => data.outputDimValues)
}

export interface OutputPage {
  type: ArrayTypeJson | null; rows: GqlArrayRow[]; total: number | null; hasMore: boolean; offset: number
}
export interface OutputQuery {
  offset: number
  first: number
  filters?: Record<string, string>
  sort?: { key: string; direction: 'asc' | 'desc' } | null
  /** Ask for the last page, whatever `offset` says. */
  last?: boolean
}

/** One page of a source's current output; filters and sorting cover the whole selected output. */
export function fetchOutputPage(source: OutputSource, { offset, first, filters = {}, sort = null, last = false }: OutputQuery,
                                signal?: AbortSignal) {
  return gql<{ outputPage: OutputPage }>(OUTPUT_PAGE,
    { source, offset, first, filters: Object.entries(filters).map(([key, text]) => ({key, text})), sort, last }, signal)
    .then((data) => data.outputPage)
}

export const OUTPUT_PAGE = `query($source: OutputSource!, $offset: Int!, $first: Int!, $filters: [ColumnFilter!], $sort: OutputSort, $last: Boolean!) {
  outputPage(source: $source, offset: $offset, first: $first, filters: $filters, sort: $sort, last: $last) { type rows { key value } total offset hasMore }
}`

/** Every spawn the selection picks out of a run, read a page of 200 at a time: what an
 *  `InvocationPanel` renderer is handed. */
export async function fetchAllSpawns(source: RunSource, selection: Record<string, KeyValue>, signal: AbortSignal) {
  const spawns: GqlSpawn[] = []
  let after: string | null = null
  do {
    const page = await fetchRunPage(source, selection, after, 200, null, signal)
    spawns.push(...page.items)
    after = page.next
  } while (after !== null)
  return spawns
}

/** The first invocation each selection picks out of a run (null where it picks out none), in one
 *  request: a table's rows each find the invocation that produced them this way. */
export async function fetchFirstSpawns(source: RunSource, selections: readonly Record<string, KeyValue>[],
                                       signal?: AbortSignal): Promise<(GqlSpawn | null)[]> {
  if (selections.length === 0) return []
  const params = selections.map((_, index) => `$s${index}: JSON!`).join(', ')
  const fields = selections.map((_, index) => `r${index}: runPage(jobId: $jobId, pathname: $pathname, nodeId: $nodeId,
    selection: $s${index}, first: 1) { items { ${SPAWN_FIELDS} } }`).join('\n')
  const variables: Record<string, unknown> = { ...source }
  selections.forEach((selection, index) => { variables[`s${index}`] = selection })
  const data = await gql<Record<string, { items: GqlSpawn[] }>>(
    `query($jobId: ID!, $pathname: String!, $nodeId: String!, ${params}) {\n${fields}\n}`, variables, signal)
  return selections.map((_, index) => {
    const page = data[`r${index}`]
    if (!page) throw new Error(`the run page query carried no answer for selection ${index}`)
    return page.items[0] ?? null
  })
}

/** A run's invocation with what it produced: its result's row count (null without one) and,
 *  when every invocation produces one row, that row's value as base64 DRISL. */
export interface InvocationItem { spawn: GqlSpawn; rowCount: number | null; value: string | null }
export interface InvocationPage { items: InvocationItem[]; total: number; offset: number }
export interface InvocationQuery {
  offset: number
  first: number
  /** By column key: `key:<dim>`, `state:`, `rows:`, and the value columns. */
  filters?: Record<string, string>
  sort?: Sort | null
  /** Only the invocations in this display state. */
  state?: string | null
  /** Ask for the last page, whatever `offset` says. */
  last?: boolean
}

/** One page of the invocations the selection picks out of a run, each with what it produced;
 *  filters and the sort cover every invocation in the selection. */
export function fetchInvocationPage(source: RunSource, selection: Record<string, KeyValue>,
                                    { offset, first, filters = {}, sort = null, state = null, last = false }: InvocationQuery,
                                    signal?: AbortSignal) {
  return gql<{ invocationPage: InvocationPage }>(`query($jobId: ID!, $pathname: String!, $nodeId: String!,
    $selection: JSON!, $offset: Int!, $first: Int!, $filters: [ColumnFilter!], $sort: OutputSort, $state: String,
    $last: Boolean!) {
    invocationPage(jobId: $jobId, pathname: $pathname, nodeId: $nodeId, selection: $selection, offset: $offset,
      first: $first, filters: $filters, sort: $sort, state: $state, last: $last) {
      items { spawn { ${SPAWN_FIELDS} } rowCount value } total offset
    }
  }`, { ...source, selection, offset, first, filters: Object.entries(filters).map(([key, text]) => ({ key, text })),
    sort, state, last }, signal).then((data) => data.invocationPage)
}

interface EntityMeta { cid: string; typeTag: string }
const metadata = new Map<string, Promise<EntityMeta | null>>()
let queued: { cid: string; resolve: (value: EntityMeta | null) => void; reject: (error: unknown) => void }[] = []
let scheduled = false

function loadMetadata(cid: string): Promise<EntityMeta | null> {
  let promise = metadata.get(cid)
  if (promise) return promise
  promise = new Promise((resolve, reject) => queued.push({ cid, resolve, reject }))
  metadata.set(cid, promise)
  if (metadata.size > 2000) metadata.delete(metadata.keys().next().value!)
  if (!scheduled) {
    scheduled = true
    setTimeout(() => {
      scheduled = false
      const batch = queued
      queued = []
      for (let i = 0; i < batch.length; i += 200) {
        const requests = batch.slice(i, i + 200)
        gql<{ entityMetas: EntityMeta[] }>('query($cids: [String!]!) { entityMetas(cids: $cids) { cid typeTag } }', { cids: requests.map((r) => r.cid) })
          .then(({ entityMetas }) => {
            const byId = new Map(entityMetas.map((meta) => [meta.cid, meta]))
            requests.forEach((request) => request.resolve(byId.get(request.cid) ?? null))
          }, (error: unknown) => requests.forEach((request) => { metadata.delete(request.cid); request.reject(error) }))
      }
    }, 0)
  }
  return promise
}

/** An entity's type metadata; idle for a null cid. */
export function useEntityMetadata(cid: string | null) {
  return useQuery(`entity-meta:${cid}`, () => loadMetadata(cid!), { enabled: cid !== null })
}

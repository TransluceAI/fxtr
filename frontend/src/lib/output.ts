import { useMemo } from 'react'
import { EntityID, type DataValue, type Spawn } from 'fxtr-view'
import { toDataValue } from 'fxtr-view-shell-util'
import { fetchOutputPage, fetchRunPage, useQuery, type OutputPage } from '../api/browse'
import { asCid, decodeBase64Cbor } from '../api/cbor'
import { invocationOf, makeRow } from '../model/level'
import { valueColumnKey } from '../model/schema'
import type { ArrayTypeJson, KeyValue, OutputSource, StepDoc, StepInvocation, TableDescriptor, TableQuery, TableRow } from '../types'
import { invocationDims, spawnOf } from './invocations'

/**
 * A node's output as its panel shows it: the rows the level's slice picks out (every row of a
 * node that was not run), read from the server a bounded page at a time. A value is the row's one
 * anonymous value cell, or its struct's cells gathered back into a map by field.
 */

/** Each page response's rows decoded, by the table they are rows of. Readers of one page share its
 *  response object (one request, and `useQuery`'s kept values), so a card, its panel, and a view
 *  mounted again decode it once. */
const decoded = new WeakMap<OutputPage, Map<string, TableRow[]>>()

/** A page's rows as table rows of `tableId`, typed by the page's type, else `fallback`. */
function decodedRows(page: OutputPage, tableId: string, fallback: ArrayTypeJson): TableRow[] {
  let byTable = decoded.get(page)
  if (!byTable) decoded.set(page, (byTable = new Map()))
  let rows = byTable.get(tableId)
  if (!rows) {
    const type = page.type ?? fallback
    rows = page.rows.map((row) => makeRow(tableId, type, row.key, decodeBase64Cbor(row.value)))
    byTable.set(tableId, rows)
  }
  return rows
}

const NO_ROWS: TableRow[] = []

/** Whether the node has anything for its panel to show: a run with invocations, or stored rows. */
export function hasOutput(step: StepDoc): boolean {
  return step.browse !== null ? (step.progress.counts?.total ?? 0) > 0 : step.output !== null
}

/** The rows of `step` the slice picks out, as an output source; null for a node with none. */
export function outputSourceFor(step: StepDoc, slice: Readonly<Record<string, KeyValue>>): OutputSource | null {
  if (!step.output) return null
  return { ...step.output, selection: { ...slice } }
}

/** Whether each value is an entity as a whole: one anonymous value column of ids. Only then can
 *  the output be shown through the entity type's own panels. */
export function isEntityOutput(step: TableDescriptor): boolean {
  const values = step.columns.filter((column) => column.group === 'value')
  if (values.length !== 1 || values[0].key !== valueColumnKey(null)) return false
  const schema = values[0].schema
  return schema === 'id' || (typeof schema === 'object' && 'id' in schema)
}

/** The entity type an entity output's schema names (`id[T]`), read without loading a row; null
 *  for an untyped id or an output that is not entities. The panel reads the type off the first
 *  row instead, so it knows it for untyped ids too. */
export function declaredOutputTypeName(step: TableDescriptor): string | null {
  if (!isEntityOutput(step)) return null
  const schema = step.columns.find((column) => column.group === 'value')!.schema
  return typeof schema === 'object' && 'id' in schema ? schema.id : null
}

/** The entity each row holds, in row order, for an entity output. Throws for a cell that is not an
 *  entity reference, which the table's schema rules out. */
export function outputIds(step: TableDescriptor, rows: readonly TableRow[]): EntityID[] {
  if (!isEntityOutput(step)) throw new Error(`${step.name}'s values are not entities`)
  return rows.map((row) => {
    const cid = asCid(row.cells[valueColumnKey(null)])
    if (!cid) throw new Error(`${step.name} row ${row.id} holds no entity reference`)
    return new EntityID(cid.toString())
  })
}

/** Each row's value as view-system data, entity references becoming `EntityID`s. */
export function outputValues(step: TableDescriptor, rows: readonly TableRow[]): DataValue[] {
  const values = step.columns.filter((column) => column.group === 'value')
  const anonymous = values.length === 1 && values[0].key === valueColumnKey(null)
  return rows.map((row) =>
    anonymous
      ? toDataValue(row.cells[values[0].key] ?? null)
      : Object.fromEntries(values.map((column) => [column.label, toDataValue(row.cells[column.key] ?? null)])),
  )
}

/** What the slice picks out of a run: how many invocations, and the first of them (all that a
 *  count, a single invocation's info, or a child's `open` needs). Idle for a node that was not run. */
export function useSelectedRun(step: StepDoc, slice: Readonly<Record<string, KeyValue>>) {
  const source = step.browse
  const query = useQuery(JSON.stringify(['selected-run', source, slice]),
    (signal) => fetchRunPage(source!, { ...slice }, null, 1, null, signal),
    // A new slice keeps showing the last one's answer until its own arrives.
    { enabled: source !== null, version: step.version, retainKey: JSON.stringify(['selected-run', source]) })
  return useMemo(() => {
    const spawn = query.value?.items[0]
    const first: StepInvocation | null = spawn ? invocationOf(spawn, invocationDims(step)) : null
    return {
      total: query.value?.total ?? null,
      first,
      firstSpawn: first && step.browse ? spawnOf(step.browse.jobId, first) : null as Spawn | null,
      error: query.error,
      reload: query.reload,
    }
  }, [query.value, query.error, query.reload, step])
}

/**
 * Up to `limit` of the rows the slice picks out, from the start of the output, and how many
 * there are when the server knows (`total`/`hasMore`).
 */
export function useOutputRows(step: StepDoc, slice: Readonly<Record<string, KeyValue>>, limit: number, enabled = true) {
  const source = outputSourceFor(step, slice)
  const query = useQuery(JSON.stringify(['output-rows', source, limit]),
    (signal) => fetchOutputPage(source!, { offset: 0, first: limit }, signal),
    { enabled: enabled && source !== null, version: source && 'jobId' in source ? step.version : undefined,
      retainKey: JSON.stringify(['output-rows', step.output, limit]) })
  const page = query.value
  const rows = page ? decodedRows(page, step.id, step.type) : null
  return { rows, total: query.value?.total ?? null, hasMore: query.value?.hasMore ?? false,
    loading: query.loading, error: query.error, reload: query.reload }
}

/**
 * One page of an output source's rows for a table: the rows its query's filters match, in its
 * order, `pageSize` at a time. While a page loads, the last one of the same source (whatever its
 * slice) stays shown. Tables reading the same page at once share the request.
 */
export function useOutputPage(table: TableDescriptor, source: OutputSource, version: string | undefined,
  { filters, sort, offset }: TableQuery, pageSize: number) {
  const query = useQuery(JSON.stringify(['output-page', source, filters, sort, offset, pageSize]), (signal) =>
    fetchOutputPage(source, { offset: offset === 'last' ? 0 : offset, first: pageSize, filters, sort, last: offset === 'last' }, signal),
    { retainKey: JSON.stringify({ ...source, selection: null }), version: 'jobId' in source ? version : undefined })
  const data = query.value
  const type = data?.type ?? table.type
  const rows = data ? decodedRows(data, table.id, table.type) : NO_ROWS
  return { data, type, rows, error: query.error, loading: query.loading, refreshing: query.refreshing, reload: query.reload }
}

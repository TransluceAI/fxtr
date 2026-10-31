import { useMemo } from 'react'
import { fetchFirstSpawns, fetchInvocationPage, useQuery } from '../api/browse'
import { decodeBase64Cbor } from '../api/cbor'
import { invocationOf, valueCells } from '../model/level'
import { columnsFor, keyColumnKey } from '../model/schema'
import type { ArrayTypeJson, Cell, Column, KeyValue, StepDoc, StepInvocation, TableRow } from '../types'
import { invocationDims, invocationSliceFor, rowSliceFor } from './invocations'
import { useLevelState, type CardMode, type CardTableState } from './levelState'
import { useStoreSelector } from './store'
import { outputSourceFor, useOutputPage } from './output'

/** How many rows a card's table shows at once. */
export const CARD_PAGE_SIZE = 10

const NO_FILTERS: Readonly<Record<string, string>> = Object.freeze({})
const NO_ROWS: TableRow[] = []

/** The stored state if it is of this slice; otherwise this slice's first pages with nothing picked,
 *  under the stored mode, filters, order, rows shown or hidden, and failed invocations listed or
 *  not. Failed invocations are listed alone only while the run has any: their switch, the card's
 *  failed count, goes away with them, and would otherwise leave the table listing none with no way
 *  back. A node that was not run lists its rows; a run whose invocations each produce one row lists
 *  its invocations, whose table holds those rows' values too. */
function forSlice(stored: CardTableState | undefined, sliceKey: string, step: StepDoc): CardTableState {
  const state: CardTableState = stored?.sliceKey === sliceKey ? stored
    : { sliceKey, mode: stored?.mode ?? 'data', filters: stored?.filters ?? NO_FILTERS, sort: stored?.sort ?? null, offset: 0,
      rowsShown: stored?.rowsShown ?? true, invocationFilters: stored?.invocationFilters ?? NO_FILTERS,
      invocationSort: stored?.invocationSort ?? null, failedOnly: stored?.failedOnly ?? false, invocationsOffset: 0, keysPick: null }
  if (step.browse === null && state.mode !== 'data') return { ...state, mode: 'data', failedOnly: false }
  if (step.browse !== null && oneRowEach(step) && state.mode !== 'invocations') return { ...state, mode: 'invocations' }
  const hasFailures = (step.progress.counts?.failed ?? 0) > 0
  return state.failedOnly && !hasFailures ? { ...state, failedOnly: false, invocationsOffset: 0 } : state
}

/** The node's rows its card's table shows (every dim of its output sliced by the level's slice,
 *  `rowSliceFor`), the table's state for that slice, and a setter of it, without reading a page. */
export function useCardTableState(step: StepDoc) {
  const { slice: levelSlice, cardTables } = useLevelState()
  const slice = useMemo(() => rowSliceFor(step, levelSlice), [step, levelSlice])
  const sliceKey = JSON.stringify(slice)
  const state = forSlice(useStoreSelector(cardTables, (tables) => tables.get(step.id)), sliceKey, step)
  const update = (patch: Partial<Omit<CardTableState, 'sliceKey'>>) =>
    cardTables.update((tables) => new Map(tables).set(step.id, { ...forSlice(tables.get(step.id), sliceKey, step), ...patch }))
  return { slice, state, update }
}

/**
 * A card's table: the slice of the node's rows it shows, its state for that slice, a setter of it,
 * and the page it shows. For a node with output only.
 */
export function useCardTable(step: StepDoc) {
  const { slice, state, update } = useCardTableState(step)
  const source = outputSourceFor(step, slice)
  if (!source) throw new Error(`${step.name} has no output for its card's table`)
  const page = useOutputPage(step, source, step.version, state, CARD_PAGE_SIZE)
  return { slice, state, update, page }
}

/** What a card lists while its table is shown (null while hidden: the card's segmented control
 *  then has nothing chosen), and the control's choice: the mode shown hides the table; any other,
 *  or any while hidden, shows the table in that mode. Switching modes lists every invocation
 *  again. */
export function useCardMode(step: StepDoc): [CardMode | null, (mode: CardMode) => void] {
  const { state, update } = useCardTableState(step)
  const shown = state.rowsShown ? state.mode : null
  return [shown, (mode) => {
    if (mode === shown) update({ rowsShown: false })
    else if (mode === state.mode) update({ rowsShown: true })
    else update({ mode, rowsShown: true, failedOnly: false, invocationsOffset: 0 })
  }]
}

/** Whether a card lists only its failed invocations, and the switch the card's failed count is:
 *  switching them on lists the invocations, with the table's rows shown if they were hidden. */
export function useFailedOnly(step: StepDoc): [boolean, () => void] {
  const { state, update } = useCardTableState(step)
  const toggle = () => update(state.failedOnly ? { failedOnly: false, invocationsOffset: 0 }
    : { mode: 'invocations', failedOnly: true, invocationsOffset: 0, rowsShown: true })
  return [state.failedOnly, toggle]
}

/** The value columns of an invocations table whose invocations each produce several rows: one,
 *  with no header, saying how many (sorted and filtered on the count). */
export const ROW_COUNT_COLUMN: Column = { key: 'rows:', label: '', group: 'value', schema: 'int', numeric: false, describes: true }

/** An invocations table's column of each invocation's display state (the backend's: `completed`,
 *  `running`, …). */
export const STATE_COLUMN: Column = { key: 'state:', label: 'state', group: 'value', schema: 'str', numeric: false }

/** Whether each invocation of a run produces one row: its output has no dim but its invocation
 *  dims. */
export function oneRowEach(step: StepDoc): boolean {
  return step.type.dims.length === invocationDims(step).length
}

/** The type of an invocations table: the run's invocation dims, in its spawn keys' order, and the
 *  output's value schema. */
export function invocationsType(step: StepDoc): ArrayTypeJson {
  const dims = invocationDims(step).map((dim) => {
    const found = step.type.dims.find(([name]) => name === dim)
    if (!found) throw new Error(`${step.name}'s output has no dim ${dim}`)
    return found
  })
  return { dims, schema: step.type.schema }
}

/** The columns of an invocations table: a key column per invocation dim, the invocation's state,
 *  then the output's value columns when each invocation produces one row, or else one column of
 *  row counts. */
export function invocationsColumns(step: StepDoc): Column[] {
  const columns = columnsFor(invocationsType(step))
  const keys = columns.filter((column) => column.group === 'key')
  return [...keys, STATE_COLUMN, ...(oneRowEach(step) ? columns.filter((column) => column.group === 'value') : [ROW_COUNT_COLUMN])]
}

/**
 * The page of invocations a card's table lists in `invocations` mode: those the level's slice
 * picks out (on the node's invocation dims; only the failed ones while `failedOnly`), under the
 * table's filters and order, one row each (`invocationPage`, which filters and sorts them all). A
 * row's cells are its invocation's keys, its state, and what it produced: its one row's values,
 * when each invocation produces one row, or else how many rows. It carries the invocation
 * (`TableRow.invocation`); a failed one carries it as its failure too (`TableRow.failure`), for
 * its error. Shaped as `useOutputPage`'s page, so the table pages it the same way. Idle while not
 * shown.
 */
export function useInvocationPage(step: StepDoc, state: CardTableState) {
  const source = step.browse
  const slice = invocationSliceFor(step, useLevelState().slice)
  const { invocationsOffset: offset, invocationFilters: filters, invocationSort: sort, failedOnly } = state
  const query = useQuery(JSON.stringify(['invocation-page', source, slice, failedOnly, filters, sort, offset, CARD_PAGE_SIZE]),
    (signal) => fetchInvocationPage(source!, slice, { offset: offset === 'last' ? 0 : offset, first: CARD_PAGE_SIZE, filters,
      sort, state: failedOnly ? 'failed' : null, last: offset === 'last' }, signal),
    { enabled: state.mode === 'invocations' && source !== null, version: step.version,
      retainKey: JSON.stringify(['invocation-page', source]) })
  const page = query.value
  const rows = useMemo(() => {
    if (!page || !source) return NO_ROWS
    const dims = invocationDims(step)
    return page.items.map(({ spawn, rowCount, value }): TableRow => {
      const invocation = invocationOf(spawn, dims)
      const cells: Record<string, Cell> = Object.fromEntries(dims.map((dim, index) => [keyColumnKey(dim), spawn.key[index]!]))
      cells[STATE_COLUMN.key] = spawn.state
      if (value !== null) Object.assign(cells, valueCells(step.type.schema, decodeBase64Cbor(value)))
      if (rowCount !== null && !oneRowEach(step)) cells[ROW_COUNT_COLUMN.key] = `${rowCount} ${rowCount === 1 ? 'row' : 'rows'}`
      return {
        id: `${step.id}#invocation#${JSON.stringify(spawn.key)}`,
        key: spawn.key,
        cells,
        invocation,
        ...(invocation.state === 'failed' ? { failure: { jobId: source.jobId, invocation } } : {}),
      }
    })
  }, [page, source, step])
  const start = page?.offset ?? 0
  const data = page && { offset: start, total: page.total, hasMore: start + page.items.length < page.total }
  return { data, rows, error: query.error, loading: query.loading, refreshing: query.refreshing, reload: query.reload }
}

/** The invocation dims' values of a row of the node's output: the invocation that produced it. */
export function rowInvocationKey(step: StepDoc, row: TableRow): Record<string, KeyValue> {
  return Object.fromEntries(invocationDims(step).map((dim) => {
    const value = row.cells[keyColumnKey(dim)]
    if (typeof value !== 'string' && typeof value !== 'number') throw new Error(`${step.name} row ${row.id} has no ${dim} key`)
    return [dim, value]
  }))
}

/**
 * The invocation that produced each row of a page of the node's output, read in one request and
 * kept current as the run moves: a lookup by row, answering undefined while loading (or for a
 * row not on the page) and null for a row whose invocation is not recorded. Idle for a node that
 * was not run, or while `enabled` is false.
 */
export function useRowInvocations(step: StepDoc, rows: readonly TableRow[], enabled: boolean) {
  const source = step.browse
  const keys = useMemo(() => {
    if (!source) return []
    const unique = new Map(rows.map((row) => {
      const key = rowInvocationKey(step, row)
      return [JSON.stringify(key), key] as const
    }))
    return [...unique.values()]
  }, [rows, source, step])
  // Paired with their keys, since an answer kept from another page while this one's load stands in.
  const query = useQuery(JSON.stringify(['row-invocations', source, keys]),
    async (signal) => new Map((await fetchFirstSpawns(source!, keys, signal)).map((spawn, index) => [JSON.stringify(keys[index]), spawn])),
    { enabled: enabled && source !== null && keys.length > 0, version: step.version,
      retainKey: JSON.stringify(['row-invocations', source]) })
  return useMemo(() => {
    const dims = invocationDims(step)
    const byKey = new Map([...query.value ?? []].map(([key, spawn]) => [key, spawn && invocationOf(spawn, dims)]))
    return (row: TableRow): StepInvocation | null | undefined => byKey.get(JSON.stringify(rowInvocationKey(step, row)))
  }, [query.value, step])
}

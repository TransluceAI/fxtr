/** Output pages use current results; filtering and sorting cover the entire sliced output. */
import { useContext, useState } from 'react'
import { useDebouncedInput } from '../lib/debounce'
import { useOutputPage } from '../lib/output'
import { OpenEntityContext, dragLink } from '../lib/openEntity'
import type { LinkTarget } from '../panes/drag'
import { columnsFor } from '../model/schema'
import type { ArrayTypeJson, OutputSource, Sort, TableDescriptor, TablePage, TableRow } from '../types'
import { RetryLine } from './Status'
import { TablePanel } from './TablePanel'
import { PAGE_SIZE, Pagination, adjacentOffset } from './Pagination'

const NO_DIMS: readonly string[] = []

/** A page of rows as `TablePanel` shows it: the columns of the page's type, less the key columns
 *  of `hiddenDims`. */
export function shownPage(table: TableDescriptor, type: ArrayTypeJson, rows: TableRow[], hiddenDims: readonly string[] = NO_DIMS): TablePage {
  return { ...table, type, dims: type.dims.map(([name]) => name),
    columns: columnsFor(type).filter((column) => column.group !== 'key' || !hiddenDims.includes(column.label)), rows }
}

function onlyValue(page: TablePage, field: string | undefined): TablePage {
  if (field === undefined) return page
  return { ...page, columns: page.columns.filter((column) => column.group === 'key' || column.label === field) }
}

/** An output source's rows a page at a time, under the pager. With
 *  `rowLink`, each row is a link from the view it is in: a click follows it, a drag carries it into
 *  the detail view's columns, and the row is lit while what it names is open. With `onSelectRow`
 *  instead, a click picks the row, and `selectedRowId` is lit. */
export function PagedTable({ table, source, version, rowLink, onSelectRow, selectedRowId, hiddenDims = NO_DIMS, hideHead = false, linkCells = false, onlyValueColumn }: {
  table: TableDescriptor; source: OutputSource; version?: string
  /** What each row opens. */
  rowLink?: (row: TableRow) => LinkTarget
  /** What clicking a row does, when it opens no link. */
  onSelectRow?: (row: TableRow) => void
  /** The row lit as picked, with `onSelectRow`. */
  selectedRowId?: string
  /** `TablePanel`'s `linkCells`. */
  linkCells?: boolean
  /** Show this one value column (by field name) and the key columns, leaving the other values out. */
  onlyValueColumn?: string
  /** Key columns left out: dims the source's slice fixes, which the surroundings name. */
  hiddenDims?: readonly string[]
  /** Leave out the table's head line (`TablePanel`'s `hideHead`). */
  hideHead?: boolean
}) {
  const [applied, setApplied] = useState<Record<string, string>>({})
  const [sort, setSort] = useState<Sort | null>(null)
  // A page is of one slice: a new slice starts at the first page (without first reading the
  // old page of it), while the rows on screen stay until its own arrive.
  const sliceKey = JSON.stringify(source.selection ?? null)
  const [paging, setPaging] = useState<{ sliceKey: string; offset: number | 'last' }>({ sliceKey, offset: 0 })
  const offset = paging.sliceKey === sliceKey ? paging.offset : 0
  const setOffset = (next: number | 'last') => setPaging({ sliceKey, offset: next })
  const typed = useDebouncedInput(applied, (filters) => { setApplied(filters); setOffset(0) })
  const page = useOutputPage(table, source, version, { filters: applied, sort, offset }, PAGE_SIZE)
  const loading = page.loading || typed.pending
  const data = page.data
  const rows = page.rows
  const opener = useContext(OpenEntityContext)
  if (rowLink && !opener) throw new Error('a table of row links was rendered outside an OpenEntityContext provider')
  const rowById = (id: string) => {
    const row = rows.find((candidate) => candidate.id === id)
    if (!row) throw new Error(`no row ${id} on the page`)
    return row
  }
  const links = rowLink && opener ? {
    onSelectRow: (id: string) => opener.follow(rowLink(rowById(id))),
    rowShown: (row: TableRow) => opener.linkState(rowLink(row)).open,
    onDragRow: (id: string, event: React.DragEvent) => dragLink(opener, event, rowLink(rowById(id))),
  } : onSelectRow ? {
    onSelectRow: (id: string) => onSelectRow(rowById(id)),
    rowShown: (row: TableRow) => row.id === selectedRowId,
  } : {}
  const turnPage = (direction: 1 | -1) => {
    const next = data && !page.error ? adjacentOffset(data, PAGE_SIZE, direction) : null
    if (next !== null) setOffset(next)
    return next !== null
  }
  return <>
    {!!page.error && <RetryLine error={page.error} onRetry={page.reload} />}
    <div className="paged-table" aria-busy={loading || page.refreshing}>
        <TablePanel table={onlyValue(shownPage(table, page.type, rows, hiddenDims), onlyValueColumn)} hideHead={hideHead} linkCells={linkCells} turnPage={turnPage} {...links}
          query={{ filters: typed.value, sort, loading, onFilters: typed.set, onSort: (next) => { setSort(next); setOffset(0) } }} />
        <div className="paged-table-footer">
          <Pagination offset={data?.offset ?? 0} count={rows.length} total={data?.total ?? null} hasMore={data?.hasMore ?? false} loading={loading} refreshing={page.refreshing} disabled={!!page.error}
            onPage={setOffset} onLast={() => setOffset('last')} />
        </div>
      </div>
  </>
}

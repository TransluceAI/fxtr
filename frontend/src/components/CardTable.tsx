import { useLayoutEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronUp } from 'lucide-react'
import { fetchRunPage, useQuery } from '../api/browse'
import { CARD_PAGE_SIZE, invocationsColumns, invocationsType, useCardTable, useCardTableState,
  useInvocationPage, useRowInvocations } from '../lib/cardTable'
import { useDebouncedInput } from '../lib/debounce'
import { useDismiss } from '../lib/dismiss'
import { levelDimList, nodeDim, useDimCount, withoutDim } from '../lib/keyLists'
import { useLevelState } from '../lib/levelState'
import { invocationTarget, nodeBase, nodeLocation, nodeTarget } from '../lib/linkBase'
import { dragLink, useEntityOpener } from '../lib/openEntity'
import { useLevelOpener } from '../lib/openLevel'
import { keyColumnKey } from '../model/schema'
import type { Column, KeyValue, StepDoc, StepInvocation, TablePage, TableRow } from '../types'
import { CardKeys } from './CardKeys'
import { KeyValueList } from './KeyValueList'
import { shownPage } from './PagedTable'
import { RetryLine } from './Status'
import { Pagination, adjacentOffset } from './Pagination'
import { TablePanel, type RowInvocation } from './TablePanel'
import { LinkBase } from 'fxtr-view'
import { cx } from '../lib/cx'
import styles from './CardTable.module.css'

interface Props {
  step: StepDoc
  /** How many invocations the level's slice picks out of the node; null while unknown or for a
   *  node that was not run. A key column's header checks whether its picked value exists only when
   *  this is zero. */
  matching: number | null
  /** A pick was made in the table (a value chosen, or a row's keys), or a panel was opened from it. */
  onPick?: () => void
}

/**
 * What a node lists at the foot of its card, in the level's slice: for a run, what its segmented
 * control chose (`CardTableState.mode`): its invocations, the rows they produced, or the values of
 * its invocation dims (`CardKeys`); for a node that was not run, its rows.
 *
 * The rows (on every dim of its output) and the invocations (on its invocation dims) are tables
 * ten a page. A chevron badge centred on the card's bottom border, shown while the card is
 * hovered, shows or hides the table's rows (`CardTableState.rowsShown`; shown until toggled); the
 * header row stays either way, so the key columns' headers slice the level with the rows hidden. A
 * table of several pages has a pager, and keeps a full page's height on its last page; a table of
 * one page is as tall as its rows, with no pager. The table's state is the card's
 * (`useCardTable`).
 *
 * The rows table: every column, sortable, the value columns filterable. Clicking a row opens the
 * invocation that produced it at the row's keys, so its panel's output is that row
 * (`invocationTarget`; a node that was not run opens its own panel there, `nodeTarget`), and
 * clicking one with the platform's primary modifier held slices the level at its keys (the rows
 * show as clickable while it is held). At each row's right end, the
 * state of the invocation that produced it, when it has one to show: a spinner while it is queued
 * or running, red once it failed (`useRowInvocations`). A row is lit only while the panel
 * its own click opened is open, never for matching keys picked elsewhere. Sliced on every dim
 * with the rows hidden, the one row's values show in the value columns' headers, under their
 * names; otherwise (several rows, or the row shown) those headers show their column's type there,
 * as an unpicked key column's does.
 *
 * The invocations table (`useInvocationPage`): a row per invocation, its keys, its state, then
 * what it produced (its one row's values when each invocation produces one row, or else how many
 * rows, in one column with no header), and at its right end a spinner while it is queued or
 * running. It sorts and filters as the rows' table does, over every invocation in the slice, with
 * filters and an order of its own. Clicking a row opens the invocation's panel (the row lit while
 * it shows), and clicking one with the primary modifier held slices the level at its keys. A failed invocation's row
 * has its error across the state and value columns (`TablePanel`). The card's failed count lists
 * the failed invocations alone (`CardTableState.failedOnly`).
 *
 * The key columns' headers are the node's slice control: each names its dim and how many values
 * it has, and below that the value the level has picked for it, or while none is the dim's kind
 * (`int` or `str`) in faint text. Clicking one floats the value columns of every key column below
 * the header row, each under its column's header (the value columns left uncovered), over the
 * rows and whatever follows the card, until a header is clicked again, a
 * click lands outside the table, or Escape is pressed: one `KeyValueList` per dim (values the other
 * picks rule out dimmed and not choosable), a `clear` cell heading the column of a picked dim. A
 * pick this node has no invocation for (made on another card) is struck through. Picks made here
 * hold the card still on screen while every card's table changes height (`setSlice`'s `anchor`).
 *
 * Its columns are sized by `lib/columnWidths.ts` (key columns one unit, value columns to their
 * content), and it scrolls sideways on its own.
 *
 * The headers' counts do not depend on the slice, so a pick elsewhere leaves them alone. The value
 * columns query only while shown, and stay mounted once opened so each keeps its search and page.
 */
export function CardTable(props: Props) {
  const { state } = useCardTableState(props.step)
  if (state.mode === 'keys')
    return (
      <div className={styles.cardTable}>
        <CardKeys step={props.step} onPick={props.onPick} />
        <RowsToggle step={props.step} />
      </div>
    )
  return <CardRows {...props} />
}

/** The badge centred on the card's bottom border, shown while the card is hovered, showing or
 *  hiding the table's rows (or the keys' values). */
function RowsToggle({ step }: { step: StepDoc }) {
  const { state, update } = useCardTableState(step)
  const shown = state.rowsShown
  return (
    <button className={styles.rowsToggle} aria-expanded={shown} aria-label={shown ? 'Hide table' : 'Show table'}
      title={shown ? 'hide table' : 'show table'} onClick={() => update({ rowsShown: !shown })}>
      {shown ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
    </button>
  )
}

/** A card's table of its rows or its invocations. */
function CardRows({ step, matching, onPick }: Props) {
  const { slice: levelSlice, setSlice, levelPath } = useLevelState()
  const { slice, state, update, page: output } = useCardTable(step)
  const invocationPage = useInvocationPage(step, state)
  const listsInvocations = state.mode === 'invocations'
  const failedOnly = listsInvocations && state.failedOnly
  const page = listsInvocations ? { ...invocationPage, type: invocationsType(step) } : output
  const opener = useEntityOpener()
  const { follow, linkState } = opener
  const [open, setOpen] = useState(false)
  const [opened, setOpened] = useState(false)
  const [layout, setLayout] = useState<ValuesLayout | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  // Each table types into its own filters, applied once typing pauses.
  const typedRows = useDebouncedInput(state.filters, (filters) => update({ filters, offset: 0 }))
  const typedInvocations = useDebouncedInput(state.invocationFilters,
    (invocationFilters) => update({ invocationFilters, invocationsOffset: 0 }))
  const typed = listsInvocations ? typedInvocations : typedRows
  const sort = listsInvocations ? state.invocationSort : state.sort
  const dims = page.type.dims.map(([dim]) => dim)
  const rows = page.rows
  const data = page.data
  const loading = page.loading || typed.pending
  const severalPages = !!data && (data.offset > 0 || data.hasMore)
  const rowsShown = state.rowsShown
  // A slice on every dim leaves at most one row, whose values the value columns' headers show while
  // the rows are hidden, so they still read.
  const oneRow = !listsInvocations && dims.every((dim) => Object.hasOwn(slice, dim)) && rows.length === 1 ? rows[0]! : null
  const rowInvocations = useRowInvocations(step, output.rows, !listsInvocations && rowsShown)
  const table: TablePage = listsInvocations
    ? { ...step, type: page.type, dims, columns: invocationsColumns(step), rows }
    : shownPage(step, page.type, rows)
  const openInvocation = (invocation: StepInvocation) => {
    follow(invocationTarget(levelPath, step, invocation.key))
    onPick?.()
  }
  // A row's right end shows whether its invocation is still running, and a data row's whether it
  // failed (an invocation's row shows its error across its cells).
  const invocationShown = (invocation: StepInvocation) => linkState(invocationTarget(levelPath, step, invocation.key)).open
  // A run's row opens the invocation that produced it narrowed to the row; any other node's, the
  // node's panel at the row.
  const rowTarget = (row: TableRow) => step.browse && step.method !== null
    ? invocationTarget(levelPath, step, rowKey(row, dims)) : nodeTarget(levelPath, step, rowKey(row, dims))
  const rowInvocation: RowInvocation | undefined = !step.browse ? undefined
    : listsInvocations ? { of: (row) => row.invocation } : { of: rowInvocations, failed: true }
  const selectRow = (id: string) => {
    const row = rowById(id)
    if (listsInvocations) {
      if (!row.invocation) throw new Error(`${step.name}'s invocation row ${id} has no invocation`)
      openInvocation(row.invocation)
      return
    }
    follow(rowTarget(row))
    onPick?.()
  }

  // A child workflow's invocation row, double-clicked, shows that invocation's level in the trace.
  const levelOpener = useLevelOpener()
  const openRowLevel = listsInvocations && step.kind === 'workflow' && levelOpener ? (id: string) => {
    const child = rowById(id).invocation?.childPathname
    if (child) levelOpener.open(child)
  } : undefined

  // A row drags what clicking it opens.
  const dragRow = (id: string, event: React.DragEvent) => {
    const row = rowById(id)
    if (listsInvocations) {
      if (!row.invocation) throw new Error(`${step.name}'s invocation row ${id} has no invocation`)
      dragLink(opener, event, invocationTarget(levelPath, step, row.invocation.key))
      return
    }
    dragLink(opener, event, rowTarget(row))
  }

  const turnPage = (direction: 1 | -1) => {
    const next = data && !page.error ? adjacentOffset(data, CARD_PAGE_SIZE, direction) : null
    if (next !== null) update(listsInvocations ? { invocationsOffset: next } : { offset: next })
    return next !== null
  }

  const pick = (next: Readonly<Record<string, KeyValue>>) => {
    setSlice(next, step.id)
    onPick?.()
  }
  const rowById = (id: string) => {
    const row = rows.find((row) => row.id === id)
    if (!row) throw new Error(`${step.name}'s table has no row ${id}`)
    return row
  }
  const toggle = () => {
    setOpen(!open)
    setOpened(true)
  }

  useDismiss(rootRef, open, () => setOpen(false))

  // The value columns hang from the foot of the header row, each under its key column's header,
  // following the columns as they are resized or scrolled sideways.
  useLayoutEffect(() => {
    const root = rootRef.current
    const head = root?.querySelector('thead')
    const scroller = head?.closest('table')?.parentElement
    if (!open || !root || !head || !scroller) return
    const measure = () => setLayout(valuesLayout(root, head))
    const observer = new ResizeObserver(measure)
    observer.observe(head)
    for (const cell of head.querySelectorAll('th[data-key-dim]')) observer.observe(cell)
    scroller.addEventListener('scroll', measure, { passive: true })
    measure()
    return () => {
      observer.disconnect()
      scroller.removeEventListener('scroll', measure)
    }
  }, [open])

  return (
    <div className={styles.cardTable} ref={rootRef} data-values-open={open ? (layout?.full ? 'full' : 'left') : undefined}>
      {!!page.error && <RetryLine error={page.error} onRetry={page.reload} className={styles.error} />}
      <div className="paged-table" aria-busy={loading || page.refreshing}>
        <LinkBase base={nodeBase(nodeLocation(levelPath, step.name))}>
        <TablePanel
          table={table}
          gridColumns
          linkCells
          rowInvocation={rowInvocation}
          rowShown={listsInvocations ? (row) => !!row.invocation && invocationShown(row.invocation)
            : (row) => linkState(rowTarget(row)).open}
          onSelectRow={selectRow}
          onOpenRow={openRowLevel}
          onDragRow={dragRow}
          turnPage={turnPage}
          onActivateRow={(id) => pick({ ...levelSlice, ...rowKey(rowById(id), dims) })}
          hideRows={!rowsShown}
          typedHeaders
          headerValues={rowsShown ? undefined : oneRow?.cells}
          // A table of several pages keeps a full page's height on its last one, so paging moves
          // nothing below it; one page (filtered down or not) is as tall as its rows.
          minRows={severalPages ? CARD_PAGE_SIZE : 0}
          hideHead
          emptyText={failedOnly ? 'no failed invocations in the slice' : listsInvocations ? 'no invocations in the slice' : undefined}
          keyHeader={{
            open,
            onToggle: toggle,
            content: (column) => <KeyHead step={step} column={column} picked={slice[column.label]} checkMissing={matching === 0} />,
          }}
          query={{ filters: typed.value, sort, loading, onFilters: typed.set,
            onSort: (next) => update(listsInvocations ? { invocationSort: next, invocationsOffset: 0 } : { sort: next, offset: 0 }) }}
        />
        </LinkBase>
        {rowsShown && severalPages && (
          <div className={cx('paged-table-footer', styles.footer)}>
            <Pagination offset={data.offset} count={rows.length} total={data.total} hasMore={data.hasMore}
              loading={loading} refreshing={page.refreshing} disabled={!!page.error} pageSize={CARD_PAGE_SIZE}
              onPage={(offset) => update(listsInvocations ? { invocationsOffset: offset } : { offset })}
              onLast={() => update(listsInvocations ? { invocationsOffset: 'last' } : { offset: 'last' })} />
          </div>
        )}
      </div>
      <RowsToggle step={step} />
      {opened && (
        <div className={styles.values} hidden={!open}
          // Key columns scrolled out of sight leave nothing to hang under.
          style={layout ? { top: layout.top, left: layout.left - 1, width: layout.width + 2,
            visibility: layout.width > 0 ? undefined : 'hidden' } : undefined}>
          {dims.map((dim, index) => {
            const list = levelDimList(nodeDim(step, dim), levelSlice)!
            const column = layout?.columns.get(dim)
            const previous = index > 0 ? layout?.columns.get(dims[index - 1]!) : undefined
            const after = previous ? previous.left + previous.width : (layout?.left ?? 0)
            return (
              <div key={JSON.stringify([step.id, dim])} className={styles.valueColumn}
                style={column ? { marginLeft: column.left - after, width: column.width } : { display: 'none' }}>
                <KeyValueList dim={dim} scope={list.scope} against={list.against} load={list.load} version={list.version}
                  enabled={open} picked={slice[dim]} onChoose={(value) => pick({ ...levelSlice, [dim]: value })}
                  onClear={() => setSlice(withoutDim(levelSlice, dim), step.id)} />
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

/** Where the value columns go, in the table's box: the foot of the header row; the span of the key
 *  columns' headers on screen (the table scrolls sideways under a fixed box), which the value
 *  columns cover and no more; whether that span is the table's whole width; and each key column's
 *  header's left edge and width. */
interface ValuesLayout {
  top: number
  left: number
  width: number
  full: boolean
  columns: ReadonlyMap<string, { left: number; width: number }>
}

function valuesLayout(root: HTMLElement, head: HTMLElement): ValuesLayout {
  const box = root.getBoundingClientRect()
  const columns = new Map<string, { left: number; width: number }>()
  for (const cell of head.querySelectorAll<HTMLElement>('th[data-key-dim]')) {
    const cellBox = cell.getBoundingClientRect()
    columns.set(cell.dataset.keyDim!, { left: cellBox.left - box.left, width: cellBox.width })
  }
  const spans = [...columns.values()]
  if (spans.length === 0) throw new Error('a card table has no key column headers to open values under')
  const left = Math.max(0, Math.min(...spans.map((span) => span.left)))
  const right = Math.min(box.width, Math.max(...spans.map((span) => span.left + span.width)))
  return { top: head.getBoundingClientRect().bottom - box.top, left, width: Math.max(0, right - left),
    full: right >= box.width - 0.5, columns }
}

/** A row's value of each dim it has (an invocation's row has only its invocation's). */
function rowKey(row: TableRow, dims: readonly string[]): Record<string, KeyValue> {
  return Object.fromEntries(dims.flatMap((dim) => {
    const value = row.cells[keyColumnKey(dim)]
    return value === undefined ? [] : [[dim, value as KeyValue]]
  }))
}

/** A key column's header: its dim's name, how many values it has, and the value picked for it. */
function KeyHead({ step, column, picked, checkMissing }: {
  step: StepDoc; column: Column; picked: KeyValue | undefined; checkMissing: boolean
}) {
  const dim = column.label
  const keyed = nodeDim(step, dim).source?.kind === 'invocations'
  const count = useDimCount(step, dim)
  // Asked only once the whole slice picks out nothing, which is when a missing value can be why.
  const present = useQuery(JSON.stringify(['key-present', step.browse, dim, picked ?? null]),
    (signal) => fetchRunPage(step.browse!, { [dim]: picked! }, null, 1, null, signal),
    { enabled: keyed && checkMissing && picked !== undefined, version: step.version })
  const missing = keyed && checkMissing && picked !== undefined && present.value?.total === 0
  return (
    <div className={styles.keyHead} title={picked === undefined ? dim : `${dim} = ${String(picked)}`}>
      <div className={styles.keyName}>
        <span>{dim}</span>
        <span className={styles.count}>{count ?? '…'}</span>
      </div>
      <div
        className={cx(styles.picked, picked !== undefined && styles.isSliced, missing && styles.isMissing, picked === undefined && styles.isUnpicked)}
        title={missing ? `no invocation of ${step.name} has ${dim} = ${String(picked)}` : undefined}
      >
        {picked === undefined ? String(column.schema) : String(picked)}
      </div>
    </div>
  )
}

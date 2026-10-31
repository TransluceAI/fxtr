import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import * as Popover from '@radix-ui/react-popover'
import { ArrowDown, ArrowUp, ArrowUpDown, Filter, LoaderCircle, Maximize2, Settings } from 'lucide-react'
import type { Cell, Column, SchemaJson, Sort, StepInvocation, TablePage, TableRow } from '../types'
import { EntityID, EntityLink, type LinkAt } from 'fxtr-view'
import { asCid } from '../api/cbor'
import { dimKind, type DimKind } from '../lib/invocations'
import { INVOCATION_ICON, KIND_ICON_STROKE } from '../lib/kindIcons'
import { useModifierHeld } from '../lib/modifierClick'
import { hasPrimaryModifier } from '../lib/keyboard'
import { isLive } from '../lib/progress'
import { toggled } from '../lib/sets'
import { clickChip, litChip, stepKey, type ChipPlace } from '../lib/tableKeys'
import { isComplex, previewValue, summarize } from '../lib/values'
import { formatArrayType, formatSchema, schemaLabel, valueColumnKey } from '../model/schema'
import { FailureLine } from './InvocationFailure'
import { PendingInvocation } from './PendingInvocation'
import { ValueModal } from './ValueModal'
import { FieldList } from './ValueView'
import { useColumnWidths } from './useColumnWidths'
import { cx } from '../lib/cx'
import styles from './TablePanel.module.css'

const EXPAND_COL_W = 24
const DIM_CLASS: Record<DimKind, string> = {
  mapped: styles.dimMapped,
  replica: styles.dimReplica,
  other: styles.dimOther,
}

/** Key column headers that stand for something the surroundings open (a card's dim values):
 *  each shows `content` in place of its label and toggles `open` when clicked, beside a button
 *  sorting by it; key columns are not filtered. Each header cell carries its dim as
 *  `data-key-dim`, for what opens to line up with it. */
export interface KeyHeader {
  content: (column: Column) => ReactNode
  open: boolean
  onToggle: () => void
}

/** The invocation each row stands for or was produced by, whose state shows at the row's right
 *  end, in the trailing cell (`InvocationMark`). */
export interface RowInvocation {
  /** The row's invocation: undefined while not known yet, null for one not recorded. */
  of: (row: TableRow) => StepInvocation | null | undefined
  /** Mark one that failed too: for rows that do not show their invocation's error themselves. */
  failed?: boolean
}

interface Props {
  table: TablePage
  /** What clicking a row does; rows are not clickable when left out, and then each has an expand
   *  button (on hover) opening it whole in a modal instead. */
  onSelectRow?: (id: string) => void
  /** What clicking a row with the platform's primary modifier held does; while it is held,
   *  the rows show as clickable (`lib/modifierClick.ts`). */
  onActivateRow?: (id: string) => void
  /** What double-clicking a row does, after the click that selected it. */
  onOpenRow?: (id: string) => void
  /** What dragging a row carries (a link, into the detail view's columns); rows are draggable
   *  only with it. */
  onDragRow?: (id: string, event: React.DragEvent) => void
  keyHeader?: KeyHeader
  /** Pad the body with empty rows to this many, so a short page takes a full page's height. */
  minRows?: number
  /** Draw the header row alone, with no body: the key headers stay usable, but the columns draw no
   *  sort or filter controls (the sort and filters stay as they are). */
  hideRows?: boolean
  /** Give the value columns' headers a second line under the name, as a key column's header has:
   *  the column's type in faint text (`schemaLabel`), or its value in `headerValues`. */
  typedHeaders?: boolean
  /** The one row's cells when the table holds a single row, each shown in its value column's
   *  header under the column's name, as a key column's header shows the value picked for it. */
  headerValues?: Readonly<Record<string, Cell>>
  /** Size the columns by `lib/columnWidths.ts`: a column being resized snaps back to its width
   *  there when near it, with a line marking it while the column is wider, and room the columns
   *  leave is an empty column's at the right end. Otherwise columns start at a plain width and
   *  stretch to fill the table. */
  gridColumns?: boolean
  /** Leave out the head line (name, invocation count, type), when the surroundings already say
   *  what the table is; its column menu moves to the right end of the header row. */
  hideHead?: boolean
  /** Whether the columns draw their sort and filter controls (with `hideRows`, they never do). */
  queryable?: boolean
  /** What an empty table says when it is not loading and no filter is set. */
  emptyText?: string
  /** Give each value cell's entity link its place in the node's output (`EntityLink`'s `at`): the
   *  column's field, and the row's keys. For a table of a node's rows under that node's
   *  `LinkBase`. */
  linkCells?: boolean
  rowInvocation?: RowInvocation
  /** Whether an open panel shows what clicking the row opened (the row's own panel, or its
   *  invocation's): the row is lit in selection blue. No other row is, whatever its keys. */
  rowShown?: (row: TableRow) => boolean
  /** Turn to the next (1) or previous (-1) page, where there is one; false at either end. `j` on
   *  the page's last row (or `k` on its first) turns it. */
  turnPage?: (direction: 1 | -1) => boolean
  className?: string
  query: { filters: Record<string, string>; sort: Sort | null; loading?: boolean;
    onFilters: (filters: Record<string, string>) => void; onSort: (sort: Sort | null) => void }
}

/**
 * A sortable, filterable grid of a table's rows with per-column widths and visibility. A run's
 * table in a job in progress says it is partial. Rows and query state come from the page reader;
 * column widths, visibility, and row expansion stay local to the grid. With `keyHeader`, the key
 * columns' headers open what it stands for instead of being filtered. Rows that are clicked (`onSelectRow`) have no expand button; the column menu then sits at the
 * right end of the header row only when the head line is left out. While focus is in the table,
 * `j`/`k` step to the next or previous row from the lit one (`rowShown`), or from a lit entity
 * chip to the next or previous one in its column (`lib/tableKeys.ts`), turning the page at its
 * edges (`turnPage`).
 *
 * A row standing for a failed invocation (`TableRow.failure`) has its key cells, then one cell
 * across the rest of the row with its error (the trailing cell red too, so the row is red from its
 * values on), and its modal shows the invocation's state, timing, whole error, and log lines.
 */
export function TablePanel({
  table,
  onSelectRow,
  onActivateRow,
  onOpenRow,
  onDragRow,
  keyHeader,
  minRows = 0,
  hideRows = false,
  typedHeaders = false,
  headerValues,
  gridColumns = false,
  hideHead = false,
  queryable = true,
  emptyText = 'no rows',
  linkCells = false,
  rowInvocation,
  rowShown,
  turnPage,
  className,
  query,
}: Props) {
  const expandable = !onSelectRow
  /** The column after the data's holding the expand buttons or the rows' invocations, and the
   *  column menu without a head. */
  const trailingColumn = expandable || !!rowInvocation || hideHead
  useModifierHeld(!!onActivateRow && !hideRows)
  const { filters, sort, onFilters: setFilters, onSort: setSort, loading } = query
  const [hidden, setHidden] = useState<ReadonlySet<string>>(new Set())
  const [expandedRowId, setExpandedRowId] = useState<string | null>(null)
  const counts = table.progress.counts
  const partial = counts && counts.done < counts.total ? counts : null

  const columns = table.columns.filter((c) => !hidden.has(c.key))
  const keyColumns = columns.filter((c) => c.group === 'key')
  const valueColumns = columns.filter((c) => c.group === 'value')
  /** Columns after the data's: the slack column, and the trailing one. */
  const extra = (gridColumns ? 1 : 0) + (trailingColumn ? 1 : 0)
  const keyKind = (col: Column): DimKind | undefined =>
    col.group === 'key' ? dimKind(table, col.label) : undefined

  const rows = table.rows

  const scroller = useRef<HTMLDivElement>(null)
  const body = useRef<HTMLTableSectionElement>(null)
  const headRow = useRef<HTMLTableRowElement>(null)
  const padding = rows.length > 0 ? Math.max(0, minRows - rows.length) : 0
  /** A padding row's height: a row's on this page, so the page is as tall as a full one. */
  const [padHeight, setPadHeight] = useState<number | undefined>(undefined)
  useLayoutEffect(() => {
    const first = body.current?.rows[0]
    if (padding > 0 && first) setPadHeight(first.getBoundingClientRect().height)
  }, [rows, padding])

  const { widthOf, guide, startResize, resize, endResize } =
    useColumnWidths({ grid: gridColumns, columns, hasRows: rows.length > 0, scroller, headRow, body,
      reserved: trailingColumn ? EXPAND_COL_W : 0 })

  const expandedRow = table.rows.find((r) => r.id === expandedRowId) ?? null

  const invocationCount = table.progress.counts?.total ?? null
  const tableType = formatArrayType(table.type)

  const toggleSort = (key: string) =>
    setSort(
      sort?.key !== key
        ? { key, direction: 'asc' }
        : sort.direction === 'asc'
          ? { key, direction: 'desc' }
          : null,
    )

  const toggleColumn = (key: string) => {
    setHidden((prev) => toggled(prev, key))
    if (!hidden.has(key)) {
      const next = { ...filters }; delete next[key]; setFilters(next)
      if (sort?.key === key) setSort(null)
    }
  }
  // While focus is in the table (a click in it focuses it), `j`/`k` step from the lit chip to the
  // next or previous one in its column, or else from the lit row to the next or previous row. Past
  // the page's edge they turn the page, and once the next one is on screen open its first row (or
  // chip in the column) going down, its last going up (`turned`).
  const turned = useRef<{ direction: 1 | -1; from: string | undefined; chip: ChipPlace | null } | null>(null)
  const openRow = (index: number) => {
    onSelectRow!(rows[index]!.id)
    body.current?.rows[index]?.scrollIntoView?.({ block: 'nearest' })
  }
  const turn = (direction: 1 | -1, chip: ChipPlace | null) => {
    if (turnPage?.(direction)) turned.current = { direction, from: rows[0]?.id, chip }
  }
  const onKeyDown = (event: React.KeyboardEvent) => {
    const direction = stepKey(event)
    if (direction === null || !body.current) return
    const chip = litChip(body.current)
    if (chip) {
      event.preventDefault()
      if (!clickChip(body.current, chip, chip.row + direction, direction)) turn(direction, chip)
      return
    }
    const lit = onSelectRow && rowShown ? rows.findIndex(rowShown) : -1
    if (lit < 0) return
    event.preventDefault()
    if (rows[lit + direction]) openRow(lit + direction)
    else turn(direction, null)
  }
  useEffect(() => {
    const pending = turned.current
    if (!pending || loading || rows.length === 0 || rows[0]!.id === pending.from || !body.current) return
    turned.current = null
    if (pending.chip) clickChip(body.current, pending.chip, 'edge', pending.direction)
    else if (onSelectRow) openRow(pending.direction === 1 ? 0 : rows.length - 1)
  }, [rows, loading])

  const columnsMenu = <ColumnsMenu columns={table.columns} hidden={hidden} shownCount={columns.length} onToggle={toggleColumn} />

  return (
    <div className={cx(styles.panel, 'table-panel', className)} tabIndex={-1}
      onKeyDown={onKeyDown}>
      {!hideHead && (
        <div className={styles.head}>
          <span className={styles.title}>{table.name}</span>
          {invocationCount !== null && <span className={styles.meta}>
            {partial ? `partial: ${partial.done} of ${partial.total} invocations` : `${invocationCount} ${invocationCount === 1 ? 'invocation' : 'invocations'}`}
          </span>}
          <span className={styles.type} title={tableType}>
            {tableType}
          </span>
          {columnsMenu}
        </div>
      )}
      <div className={cx(styles.scroll, 'table-scroll', hideRows && styles.isHeadOnly)} ref={scroller}>
        <table className={cx(styles.table, onSelectRow && styles.isSelectable, onActivateRow && styles.isActivatable,
          hideRows && styles.isHeadOnly)}>
          <colgroup>
            {columns.map((col) => (
              <col key={col.key} style={{ width: widthOf(col) }} />
            ))}
            {gridColumns && <col />}
            {trailingColumn && <col style={{ width: EXPAND_COL_W }} />}
          </colgroup>
          <thead>
            <tr ref={headRow}>
              {columns.map((col, index) => (
                <HeaderCell
                  key={col.key}
                  column={col}
                  keyKind={keyKind(col)}
                  keyHeader={col.group === 'key' ? keyHeader : undefined}
                  queryable={queryable && !hideRows}
                  secondLine={col.group !== 'value' ? undefined
                    : headerValues ? { cell: headerValues[col.key] ?? null }
                    : typedHeaders && !col.describes ? { schema: col.schema } : undefined}
                  sort={sort}
                  filter={filters[col.key] ?? ''}
                  onSort={() => toggleSort(col.key)}
                  onFilter={(value) => setFilters({ ...filters, [col.key]: value })}
                  onResizeStart={() => startResize(col, headRow.current!.cells[index]!.offsetLeft)}
                  onResize={(delta) => resize(col, delta)}
                  onResizeEnd={() => endResize(col)}
                />
              ))}
              {gridColumns && <th className={cx(styles.thValue, styles.filler)} aria-hidden />}
              {/* Without the head line, the column menu sits at the right end of the header row. */}
              {trailingColumn && <th className={cx(styles.thValue, styles.thExpand)}>{hideHead && columnsMenu}</th>}
            </tr>
          </thead>
          {!hideRows && <tbody ref={body}>
            {rows.map((row) => (
              <tr
                key={row.id}
                data-row
                data-failure={row.failure ? '' : undefined}
                data-selected={rowShown?.(row) ? '' : undefined}
                onClick={(onSelectRow || onActivateRow) && ((event) => {
                  if (onActivateRow && hasPrimaryModifier(event)) onActivateRow(row.id)
                  else onSelectRow?.(row.id)
                })}
                onDoubleClick={onOpenRow && (() => onOpenRow(row.id))}
                draggable={onDragRow !== undefined}
                onDragStart={onDragRow && ((event) => onDragRow(row.id, event))}
              >
                {(row.failure ? keyColumns : columns).map((col) => (
                  <td key={col.key} className={cellClass(col, row)}>
                    <CellSummary value={row.cells[col.key] ?? null} at={linkCells && col.group === 'value' ? cellPlace(table, row, col) : undefined} />
                  </td>
                ))}
                {/* A failure takes the rest of the row, the slack column's room too. */}
                {row.failure && valueColumns.length + (gridColumns ? 1 : 0) > 0 && (
                  <td colSpan={valueColumns.length + (gridColumns ? 1 : 0)} className={styles.tdFailure}>
                    <FailureLine jobId={row.failure.jobId} invocation={row.failure.invocation} />
                  </td>
                )}
                {gridColumns && !row.failure && <td className={styles.filler} />}
                {trailingColumn && (
                  <td className={cx(styles.tdExpand, row.failure && styles.tdFailure)}>
                    {rowInvocation && <InvocationMark row={rowInvocation} invocation={rowInvocation.of(row)} />}
                    {expandable && !rowInvocation && (
                      <button
                        className={cx('icon-button', styles.rowExpand)}
                        aria-label={`Expand ${row.id}`}
                        onClick={(e) => {
                          e.stopPropagation()
                          setExpandedRowId(row.id)
                        }}
                      >
                        <Maximize2 size={11} />
                      </button>
                    )}
                  </td>
                )}
              </tr>
            ))}
            {Array.from({ length: padding }, (_, index) => (
              <tr key={`pad-${index}`} className={styles.padRow} style={{ height: padHeight }} aria-hidden>
                <td colSpan={columns.length + extra}>&nbsp;</td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td className={styles.empty} colSpan={columns.length + extra}>
                  {loading ? 'loading rows…' : queryable && Object.values(filters).some(text => text.trim()) ? 'no rows match the filters' : emptyText}
                </td>
              </tr>
            )}
          </tbody>}
        </table>
        {/* Where a column wider than its default snaps back to, while it is resized. */}
        {guide && <div className={styles.snapGuide} style={{ left: guide.left }} aria-hidden />}
      </div>
      {expandedRow && (
        <ValueModal
          open
          onOpenChange={(open) => !open && setExpandedRowId(null)}
          title={expandedRow.key.length ? expandedRow.key.join(' · ') : table.name}
          subtitle={expandedRow.failure ? 'failed' : tableType}
        >
          {expandedRow.failure ? (
            <PendingInvocation jobId={expandedRow.failure.jobId} invocation={expandedRow.failure.invocation} />
          ) : (
            <FieldList columns={table.columns} row={expandedRow} />
          )}
        </ValueModal>
      )}
    </div>
  )
}

/** The gear opening the list of the table's columns, each a checkbox showing or hiding it. */
function ColumnsMenu({ columns, hidden, shownCount, onToggle }: {
  columns: readonly Column[]; hidden: ReadonlySet<string>; shownCount: number; onToggle: (key: string) => void
}) {
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button className={cx('icon-button', styles.columnsButton)} aria-label="Configure columns">
          <Settings size={13} />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content className="popover" side="bottom" align="end" sideOffset={4}>
          <div className={cx('eyebrow', styles.popoverTitle)}>Columns</div>
          {columns.map((col) => (
            <label key={col.key} className={styles.checkRow}>
              <input
                type="checkbox"
                checked={!hidden.has(col.key)}
                // The last visible column cannot be turned off; a table with
                // no columns has nothing to click on.
                disabled={!hidden.has(col.key) && shownCount === 1}
                onChange={() => onToggle(col.key)}
              />
              {col.label}
            </label>
          ))}
          <Popover.Arrow className="popover-arrow" />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  )
}

/** Where a value cell sits in its node's output: its field of the row's value (the whole value for
 *  the anonymous column), at the row's keys. */
function cellPlace(table: TablePage, row: TableRow, col: Column): LinkAt {
  const keys = Object.fromEntries(table.type.dims.map(([dim], index) => [dim, row.key[index]!]))
  return { path: col.key === valueColumnKey(null) ? [] : [col.label], keys }
}

/** A row's invocation at its right end, while it has something to say: a spinner while it is
 *  queued or running, and with `row.failed`, the invocation icon in red once it failed
 *  (`INVOCATION_ICON`); nothing otherwise, or while the invocation is not known yet or was never
 *  scheduled. */
function InvocationMark({ row, invocation }: { row: RowInvocation; invocation: StepInvocation | null | undefined }) {
  if (!invocation) return null
  const label = `Invocation ${invocation.state}`
  if (isLive(invocation.state))
    return (
      <span className={styles.invocationMark} role="status" aria-label={label} title={label}>
        <LoaderCircle size={12} className={styles.spinner} aria-hidden />
      </span>
    )
  if (!row.failed || invocation.state !== 'failed') return null
  return (
    <span className={cx(styles.invocationMark, styles.isFailed)} role="status" aria-label={label} title={label}>
      <INVOCATION_ICON size={12} strokeWidth={KIND_ICON_STROKE} aria-hidden />
    </span>
  )
}

/** A cell in the grid: an entity link, container contents, or a scalar on one line. */
function CellSummary({ value, at }: { value: Cell; at?: LinkAt }) {
  const cid = asCid(value)
  if (cid) return <EntityLink id={new EntityID(cid.toString())} {...(at ? { at } : {})} />
  if (isComplex(value)) {
    const count = Array.isArray(value) ? value.length : Object.keys(value).length
    const label = summarize(value)
    return (
      <span className={styles.cellSummary}>
        <span className={styles.cellPreview}>{previewValue(value)}</span>
        <span className={styles.cellCount} title={label} aria-label={label}>{count}</span>
      </span>
    )
  }
  return <>{summarize(value)}</>
}

interface HeaderCellProps {
  column: Column
  keyKind?: DimKind
  keyHeader?: KeyHeader
  /** Whether the column draws its sort and filter controls. */
  queryable: boolean
  /** A line under the column's name: a value, or the column's type. */
  secondLine?: { cell: Cell } | { schema: SchemaJson }
  sort: Sort | null
  filter: string
  onSort: () => void
  onFilter: (value: string) => void
  onResize: (delta: number) => void
  onResizeStart: () => void
  onResizeEnd: () => void
}

function HeaderCell({
  column,
  keyKind,
  keyHeader,
  queryable,
  secondLine,
  sort,
  filter,
  onSort,
  onFilter,
  onResize,
  onResizeStart,
  onResizeEnd,
}: HeaderCellProps) {
  const dragStart = useRef<number | null>(null)

  // Drag on the trailing edge resizes; the pointer is captured so the drag keeps
  // tracking once it leaves the handle.
  const handlePointerDown = (e: React.PointerEvent) => {
    e.stopPropagation()
    dragStart.current = e.clientX
    e.currentTarget.setPointerCapture(e.pointerId)
    onResizeStart()
  }
  const handlePointerMove = (e: React.PointerEvent) => {
    if (dragStart.current === null) return
    onResize(e.clientX - dragStart.current)
    dragStart.current = e.clientX
  }
  const endDrag = () => {
    if (dragStart.current !== null) onResizeEnd()
    dragStart.current = null
  }

  const active = sort?.key === column.key
  const thClass = cx(column.group === 'key' ? styles.thKey : styles.thValue, keyKind && DIM_CLASS[keyKind])

  const grip = (
    <span
      className={styles.thGrip}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
    />
  )
  if (keyHeader)
    return (
      <th className={thClass} data-key-dim={column.label}>
        <div className={cx(styles.thInner, styles.isKeyHeader)}>
          <button className={styles.thKeyToggle} aria-expanded={keyHeader.open}
            aria-label={`${keyHeader.open ? 'Hide' : 'Show'} ${column.label} values`} onClick={keyHeader.onToggle}>
            {keyHeader.content(column)}
          </button>
          {queryable && (
            <button className={cx('icon-button', styles.thSortIcon, active && styles.isOn)} aria-label={`Sort by ${column.label}`} onClick={onSort}>
              {active ? (sort.direction === 'asc' ? <ArrowUp size={11} /> : <ArrowDown size={11} />) : <ArrowUpDown size={11} />}
            </button>
          )}
          {grip}
        </div>
      </th>
    )
  return (
    <th className={thClass}>
      <div className={cx(styles.thInner, secondLine && styles.hasValue)}>
        {queryable ? (
          <button className={styles.thSort} onClick={onSort}>
            <span className={styles.thLabel} data-fit>{column.label}</span>
            {active && (sort.direction === 'asc' ? <ArrowUp size={11} /> : <ArrowDown size={11} />)}
          </button>
        ) : (
          // The sort button's box, as a label.
          <span className={cx(styles.thSort, styles.isStatic)}>
            <span className={styles.thLabel} data-fit>{column.label}</span>
          </span>
        )}
        {/* Beside the sort button rather than in it, since the value may be an entity link. */}
        {secondLine && ('cell' in secondLine ? (
          <span className={styles.thValueLine} data-fit>
            <CellSummary value={secondLine.cell} />
          </span>
        ) : (
          <span className={cx(styles.thValueLine, styles.isType)} title={formatSchema(secondLine.schema)} data-fit>
            {schemaLabel(secondLine.schema)}
          </span>
        ))}
        {queryable && <Popover.Root>
          <Popover.Trigger asChild>
            <button
              className={cx('icon-button', styles.thFilter, filter && styles.isOn)}
              aria-label={`Filter ${column.label}`}
            >
              <Filter size={11} />
            </button>
          </Popover.Trigger>
          <Popover.Portal>
            <Popover.Content className="popover" side="bottom" align="end" sideOffset={4}>
              <input
                className={styles.filterInput}
                autoFocus
                value={filter}
                placeholder={`Filter ${column.label}…`}
                onChange={(e) => onFilter(e.target.value)}
              />
              <Popover.Arrow className="popover-arrow" />
            </Popover.Content>
          </Popover.Portal>
        </Popover.Root>}
        {grip}
      </div>
    </th>
  )
}

const cellClass = (col: Column, row: TableRow) =>
  cx(
    col.group === 'key' && styles.tdKey,
    col.numeric && styles.tdNumeric,
    col.describes && styles.tdDescribes,
    isComplex(row.cells[col.key] ?? null) && styles.tdComplex,
  )

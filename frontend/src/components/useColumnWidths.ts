import { useLayoutEffect, useState, type RefObject } from 'react'
import { defaultWidths } from '../lib/columnWidths'
import type { Column } from '../types'
import styles from './TablePanel.module.css'

/** A column's width in a table not sized as a grid (the panes' tables stretch these to fill). */
const UNSIZED_W = 130
const MIN_COL_W = 60
/** How near its default width a column being resized snaps to it. */
const SNAP_PX = 14
/** What a header or cell adds around the content measured in it: a value header's padding; a
 *  cell's padding; a container's count badge. */
const HEAD_CHROME = 20
const CELL_CHROME = 26
const COUNT_BADGE_W = 24

interface Options {
  /** Size the columns by `lib/columnWidths.ts`, rather than at a plain width. */
  grid: boolean
  /** The columns shown, in order. */
  columns: readonly Column[]
  /** Whether the page on screen has rows, which is when the columns' content is measured. */
  hasRows: boolean
  scroller: RefObject<HTMLElement>
  headRow: RefObject<HTMLTableRowElement>
  /** The body, whose data rows are marked `data-row` (and a failed invocation's, which has no
   *  value cells to measure, `data-failure`). */
  body: RefObject<HTMLTableSectionElement>
  /** The width of fixed columns after the sized ones (the expand buttons'), left out of the width
   *  the defaults share. */
  reserved: number
}

/**
 * A table's column widths: each column's default width (`defaultWidths`, from the table's width
 * and the width of each column's content, measured once, from the first page with rows other than
 * failed invocations', so paging does not move them), or the width it was resized to by hand. A column being resized snaps to its
 * default within `SNAP_PX`, and while it is wider than its default, `guide` is where that is.
 */
export function useColumnWidths({ grid, columns, hasRows, scroller, headRow, body, reserved }: Options) {
  const [resized, setResized] = useState<Record<string, number>>({})
  const [content, setContent] = useState<Record<string, number>>({})
  const [tableWidth, setTableWidth] = useState(0)
  /** The column being resized, and its left edge in the table. */
  const [resizing, setResizing] = useState<{ key: string; left: number } | null>(null)

  useLayoutEffect(() => {
    const element = scroller.current
    if (!grid || !element) return
    // `clientWidth` is rounded, possibly up: the laid-out width, floored, is never more than there is.
    const measure = () => setTableWidth(Math.floor(Math.min(element.clientWidth, element.getBoundingClientRect().width)))
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    measure()
    return () => observer.disconnect()
  }, [grid])

  // Only value columns' content decides their width.
  const measured = columns.filter((column) => column.group === 'value')
  // After every render, but it measures only columns not measured yet, once rows are on screen.
  useLayoutEffect(() => {
    const head = headRow.current
    const bodyEl = body.current
    const unmeasured = measured.filter((column) => content[column.key] === undefined)
    if (!grid || !hasRows || !head || !bodyEl || unmeasured.length === 0) return
    const rows = Array.from(bodyEl.querySelectorAll<HTMLTableRowElement>(':scope > tr[data-row]:not([data-failure])'))
    // A page of failed invocations alone says nothing of the values: wait for one with rows.
    if (rows.length === 0) return
    const next = { ...content }
    for (const column of unmeasured) {
      const index = columns.indexOf(column)
      next[column.key] = Math.max(
        contentWidth(head.cells[index]!, '[data-fit]') + HEAD_CHROME,
        ...rows.map((row) => cellWidth(row.cells[index]!)),
      )
    }
    setContent(next)
  })

  const defaults = grid
    ? defaultWidths(columns.map((column) => ({ key: column.key, group: column.group, content: content[column.key] })), Math.max(0, tableWidth - reserved))
    : null
  const defaultOf = (column: Column) => defaults?.get(column.key) ?? UNSIZED_W
  const snapped = (column: Column, width: number) =>
    defaults && Math.abs(width - defaultOf(column)) <= SNAP_PX ? defaultOf(column) : width
  const widthOf = (column: Column) => {
    const width = resized[column.key]
    return width === undefined ? defaultOf(column) : snapped(column, width)
  }

  const resizingColumn = resizing && columns.find((column) => column.key === resizing.key)
  const guide = defaults && resizing && resizingColumn && (resized[resizing.key] ?? 0) > defaultOf(resizingColumn)
    ? { left: resizing.left + defaultOf(resizingColumn) }
    : null

  return {
    widthOf,
    guide,
    startResize: (column: Column, left: number) => setResizing({ key: column.key, left }),
    /** The drag's own width accumulates; the column shows it snapped. */
    resize: (column: Column, delta: number) =>
      setResized((current) => ({ ...current, [column.key]: Math.max(MIN_COL_W, (current[column.key] ?? widthOf(column)) + delta) })),
    /** Let go, the column keeps the width it shows. */
    endResize: (column: Column) => {
      setResizing(null)
      setResized((current) => current[column.key] === undefined ? current : { ...current, [column.key]: snapped(column, current[column.key]) })
    },
  }
}

/** A cell's content width, with its padding (and a container's count badge). */
function cellWidth(cell: HTMLTableCellElement): number {
  return contentWidth(cell) + CELL_CHROME + (cell.classList.contains(styles.tdComplex) ? COUNT_BADGE_W : 0)
}

/** How wide an element's content lays out, however narrow the element (a clipped cell's text
 *  still lays out whole): of the element, or of the widest of its descendants matching
 *  `selector`. */
function contentWidth(element: Element, selector?: string): number {
  const targets = selector ? Array.from(element.querySelectorAll(selector)) : [element]
  let width = 0
  for (const target of targets) {
    const range = document.createRange()
    range.selectNodeContents(target)
    width = Math.max(width, range.getBoundingClientRect().width)
  }
  return width
}

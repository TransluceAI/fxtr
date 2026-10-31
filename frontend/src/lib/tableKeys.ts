import type { KeyboardEvent as ReactKeyboardEvent } from 'react'
import { isEditableTarget } from './keyboard'

/** Which way a keystroke steps through a table's rows: `j` down (1), `k` up (-1); null for any
 *  other key, one with a modifier, or one typed into a field. */
export function stepKey(event: KeyboardEvent | ReactKeyboardEvent): 1 | -1 | null {
  if (event.metaKey || event.ctrlKey || event.altKey || isEditableTarget(event.target)) return null
  return event.key === 'j' ? 1 : event.key === 'k' ? -1 : null
}

/** What every entity chip carries (`EntityLinkImpl`), and its value while the browsing panel after
 *  its view shows what it names. */
export const CHIP_ATTRIBUTE = 'data-entity-link'
export const CHIP_SELECTED = 'selected'

/** Where a chip sits in a table's rows: its row (among `chipRows`), its column (the cell's
 *  index), and its place among the chips in its cell. */
export interface ChipPlace {
  row: number
  column: number
  position: number
}

/** The rows of a table body that can hold chips: its data rows, less a failed invocation's
 *  (whose cells are not the columns'). */
function chipRows(body: HTMLTableSectionElement): HTMLTableRowElement[] {
  return Array.from(body.querySelectorAll<HTMLTableRowElement>('tr[data-row]:not([data-failure])'))
}

function chipsIn(cell: Element): HTMLElement[] {
  return Array.from(cell.querySelectorAll<HTMLElement>(`[${CHIP_ATTRIBUTE}]`))
}

/** The chip in `body` that the browsing panel after its view shows; null for none. */
export function litChip(body: HTMLTableSectionElement): ChipPlace | null {
  const lit = body.querySelector<HTMLElement>(`[${CHIP_ATTRIBUTE}="${CHIP_SELECTED}"]`)
  const cell = lit?.closest('td')
  const row = cell?.parentElement
  if (!lit || !cell || !(row instanceof HTMLTableRowElement)) return null
  return { row: chipRows(body).indexOf(row), column: cell.cellIndex, position: chipsIn(cell).indexOf(lit) }
}

/**
 * Click the chip in `column` of the first row from `from` on (`'edge'`: the first row going down,
 * the last going up), stepping `direction`, that has one there: at `position` in its cell where it
 * can be, else its cell's last. Rows without a chip there are passed over. False when no row on
 * the way has one.
 */
export function clickChip(body: HTMLTableSectionElement, { column, position }: Omit<ChipPlace, 'row'>,
  from: number | 'edge', direction: 1 | -1): boolean {
  const rows = chipRows(body)
  const start = from !== 'edge' ? from : direction === 1 ? 0 : rows.length - 1
  for (let index = start; index >= 0 && index < rows.length; index += direction) {
    const cell = rows[index]!.cells[column]
    const chips = cell ? chipsIn(cell) : []
    if (chips.length === 0) continue
    const chip = chips[Math.min(position, chips.length - 1)]!
    chip.click()
    chip.scrollIntoView?.({ block: 'nearest' })
    return true
  }
  return false
}

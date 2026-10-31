// @vitest-environment jsdom
import {expect, it, vi} from 'vitest'
import {CHIP_ATTRIBUTE, CHIP_SELECTED, clickChip, litChip} from '../src/lib/tableKeys'

/** A table body whose rows hold, per column, the given number of chips (`-` for a failed row). */
function body(rows: (number[] | '-')[]): HTMLTableSectionElement {
  const table = document.createElement('table')
  const tbody = table.createTBody()
  for (const cells of rows) {
    const row = tbody.insertRow()
    row.setAttribute('data-row', '')
    if (cells === '-') {
      row.setAttribute('data-failure', '')
      row.insertCell()
      continue
    }
    for (const count of cells) {
      const cell = row.insertCell()
      for (let i = 0; i < count; i++) {
        const chip = document.createElement('button')
        chip.setAttribute(CHIP_ATTRIBUTE, '')
        cell.append(chip)
      }
    }
  }
  return tbody
}

const chip = (tbody: HTMLTableSectionElement, row: number, col: number, index = 0) =>
  tbody.rows[row]!.cells[col]!.querySelectorAll<HTMLElement>(`[${CHIP_ATTRIBUTE}]`)[index]!

/** `j`/`k` from the lit chip, as `TablePanel` steps: false at the page's edge. */
function step(tbody: HTMLTableSectionElement, direction: 1 | -1): boolean {
  const lit = litChip(tbody)!
  return clickChip(tbody, lit, lit.row + direction, direction)
}

it('clicks the chip in the same column of the next row that has one, at the same place in its cell', () => {
  const tbody = body([[1, 2], [1, 0], '-', [0, 3], [1, 1]])
  chip(tbody, 0, 1, 1).setAttribute(CHIP_ATTRIBUTE, CHIP_SELECTED)
  expect(litChip(tbody)).toEqual({row: 0, column: 1, position: 1})
  const clicked = vi.fn()
  tbody.addEventListener('click', (event) => clicked(event.target))
  expect(step(tbody, 1)).toBe(true)
  expect(clicked).toHaveBeenLastCalledWith(chip(tbody, 3, 1, 1))
})

it('clicks a shorter cell\'s last chip, and says when no row on the way has one', () => {
  const tbody = body([[0, 1], [0, 3]])
  chip(tbody, 1, 1, 2).setAttribute(CHIP_ATTRIBUTE, CHIP_SELECTED)
  const clicked = vi.fn()
  tbody.addEventListener('click', (event) => clicked(event.target))
  expect(step(tbody, -1)).toBe(true)
  expect(clicked).toHaveBeenLastCalledWith(chip(tbody, 0, 1, 0))
  clicked.mockClear()
  expect(step(tbody, 1)).toBe(false)
  expect(clicked).not.toHaveBeenCalled()
  expect(litChip(body([[1]]))).toBeNull()
})

it('starts from the edge of a new page: its first row with a chip going down, its last going up', () => {
  const tbody = body([[0, 0], [0, 2], [0, 1], [0, 0]])
  const clicked = vi.fn()
  tbody.addEventListener('click', (event) => clicked(event.target))
  expect(clickChip(tbody, {column: 1, position: 1}, 'edge', 1)).toBe(true)
  expect(clicked).toHaveBeenLastCalledWith(chip(tbody, 1, 1, 1))
  expect(clickChip(tbody, {column: 1, position: 1}, 'edge', -1)).toBe(true)
  expect(clicked).toHaveBeenLastCalledWith(chip(tbody, 2, 1, 0))
})

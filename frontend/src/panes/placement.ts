/**
 * Where a link opens its panel when clicked.
 *
 * A link clicked in a panel opens below that panel when the panel's column has room left for it,
 * else at the end of the column to its right when that one has room, else in a new column inserted
 * right of the clicked panel's. A link clicked outside any panel (the trace, the rail) treats the
 * columns as if clicked in the last one, at its end. Whether a click opens a panel at all, or
 * navigates the browsing panel after the view instead, is `useColumns`'s.
 *
 * A panel's height is known only once it has rendered and loaded, so whether a new one fits is
 * judged on an estimate per kind of address (`estimateHeight`).
 */

import type { Address } from './address'
import { locate, type Columns, type Placement } from './columns'

/** The space around and between panels in a column, in px. */
export const PANEL_GAP = 10

/** What a click does: open a panel at a placement, or navigate the view's open child. */
export type Decision = Placement | { kind: 'existing'; panelId: string }

/** The columns' heights as laid out now: every column is as tall as the detail view, and each
 *  open panel as tall as it measures. */
export interface Geometry {
  columnHeight: number
  panelHeight: (panelId: string) => number
}

function estimateHeight(address: Address | null): number {
  switch (address?.location?.root) {
    case 'step':
      return address.location.path.length > 1 ? 240 : 360
    case 'entity':
      return 300
    default:
      return 480
  }
}

function freeSpace(columns: Columns, column: number, geometry: Geometry): number {
  const stack = columns[column]?.panels ?? []
  const used = stack.reduce((sum, panel) => sum + geometry.panelHeight(panel.id) + PANEL_GAP, PANEL_GAP)
  return geometry.columnHeight - used
}

export function autoPlacement(columns: Columns, geometry: Geometry, originId: string | null, address: Address | null): Placement {
  const needed = estimateHeight(address) + PANEL_GAP
  const origin = originId === null ? null : locate(columns, originId)
  if (!origin) {
    const last = columns.length - 1
    if (last >= 0 && freeSpace(columns, last, geometry) >= needed)
      return { kind: 'into', column: last, index: columns[last]!.panels.length }
    return { kind: 'newColumn', index: columns.length }
  }
  if (freeSpace(columns, origin.column, geometry) >= needed)
    return { kind: 'into', column: origin.column, index: origin.index + 1 }
  const right = origin.column + 1
  if (right < columns.length && freeSpace(columns, right, geometry) >= needed)
    return { kind: 'into', column: right, index: columns[right]!.panels.length }
  return { kind: 'newColumn', index: right }
}

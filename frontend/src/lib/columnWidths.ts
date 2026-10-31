/**
 * The widths a card table's columns start at, before any is resized by hand. They are sized from
 * one unit, `max(MIN_UNIT, table width / UNITS_ACROSS)`, the same in every card of the trace
 * since they are as wide as each other. Key columns are one unit each, so the width of a card's
 * key block counts its dims against every other card's. A value column is as wide as its content
 * (header or cells), within `MIN_VALUE_W` and `MAX_VALUE_UNITS` units. Room left over is shared
 * equally by the value columns their content still does not fit, and otherwise stays empty at the
 * right.
 *
 * A column's content width is what `TablePanel` measured of it, if it has (it measures once, from
 * the first page with rows); until then a value column is one unit wide.
 */
export const MIN_UNIT = 100
export const UNITS_ACROSS = 6
export const MIN_VALUE_W = 60
export const MAX_VALUE_UNITS = 3

export interface SizedColumn {
  key: string
  group: 'key' | 'value'
  /** The width its header and cells need, once measured. */
  content: number | undefined
}

/** Whole pixels, so columns that fill the table add up to no more than it. */
export function unitWidth(tableWidth: number): number {
  return Math.max(MIN_UNIT, Math.floor(tableWidth / UNITS_ACROSS))
}

/** Each column's width before any is resized by hand, by column key. */
export function defaultWidths(columns: readonly SizedColumn[], tableWidth: number): Map<string, number> {
  const unit = unitWidth(tableWidth)
  const widths = new Map(columns.map((column) => [column.key,
    column.group === 'key' ? unit : clamp(column.content ?? unit, MIN_VALUE_W, MAX_VALUE_UNITS * unit)]))
  const cut = columns.filter((column) => column.group === 'value' && (column.content ?? 0) > widths.get(column.key)!)
  const slack = tableWidth - [...widths.values()].reduce((total, width) => total + width, 0)
  if (slack > 0 && cut.length > 0) {
    const share = Math.floor(slack / cut.length)
    for (const column of cut) widths.set(column.key, widths.get(column.key)! + share)
  }
  return widths
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, Math.round(value)))
}

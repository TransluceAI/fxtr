import { shortCid } from 'fxtr-view'
import { asCid, isBytes, isList, isStruct } from '../api/cbor'
import type { Cell } from '../types'

/** Structured values get a summary in the cell and their detail in the expand modal. */
export function isComplex(value: Cell): value is Cell[] | { [key: string]: Cell } {
  return isList(value) || isStruct(value)
}

/** Values short enough to sit beside their label instead of taking a line of their own. */
export function isInline(value: Cell): boolean {
  if (isComplex(value) || isBytes(value)) return false
  if (typeof value !== 'string') return true
  return value.length <= 48 && !value.includes('\n')
}

/** Table text follows debug_web/table_values.py: ECMAScript number formatting, exact bigint
 * decimals, JSON-quoted preview strings, and JavaScript map property order. Shared CBOR
 * fixtures in test/fixtures/table-values.json verify both implementations. */
export function summarize(value: Cell): string {
  if (value === null || value === undefined) return '—'
  const cid = asCid(value)
  if (cid) return `<${shortCid(cid.toString())}>`
  if (isBytes(value)) return `${value.length} bytes`
  if (isList(value)) return `${value.length} ${value.length === 1 ? 'item' : 'items'}`
  if (isStruct(value)) {
    const n = Object.keys(value).length
    return `{${n} ${n === 1 ? 'field' : 'fields'}}`
  }
  return String(value)
}

/** Container contents on one line; the table clips the text to the column's width. */
export function previewValue(value: Cell): string {
  if (value === null) return 'null'
  if (typeof value === 'string') return JSON.stringify(value)
  if (isList(value)) return `[${value.map(previewValue).join(', ')}]`
  if (isStruct(value)) {
    const fields = Object.entries(value).map(([key, item]) => `${JSON.stringify(key)}: ${previewValue(item)}`)
    return `{${fields.join(', ')}}`
  }
  return summarize(value)
}

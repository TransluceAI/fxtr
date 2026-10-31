import { EntityID, EntityLink } from 'fxtr-view'
import { asCid, isBytes, isList, isStruct, toHex } from '../api/cbor'
import { formatSchema } from '../model/schema'
import type { Cell, Column, TableRow } from '../types'
import { cx } from '../lib/cx'
import styles from './ValueView.module.css'

/**
 * One value at full size, by its shape: an entity id as a link (labelled and previewed by the view
 * system), bytes as a hex preview, a struct or list as a tree, else text. `clamp` caps a tree at a
 * fixed height and scrolls inside, for use in a pane.
 */
export function ValueView({ value, clamp = false }: { value: Cell; clamp?: boolean }) {
  if (value === null || value === undefined) return <span className="detail-none">null</span>
  const cid = asCid(value)
  if (cid) return <EntityLink id={new EntityID(cid.toString())} />
  if (isBytes(value)) return <Bytes value={value} />
  if (isStruct(value) || isList(value))
    return (
      <div className={cx('value-box', styles.tree, clamp && styles.isClamped)}>
        <CellTree value={value} />
      </div>
    )
  return <div className={styles.text}>{String(value)}</div>
}

/** A struct or list as nested key/value rows, entity ids as links throughout. */
export function CellTree({ value }: { value: Cell }) {
  if (isStruct(value)) {
    const entries = Object.entries(value)
    if (entries.length === 0) return <span className="detail-none">{'{}'}</span>
    return (
      <dl className={styles.struct}>
        {entries.map(([key, item]) => (
          <div key={key} className={styles.entry}>
            <dt>{key}</dt>
            <dd>
              <CellTree value={item} />
            </dd>
          </div>
        ))}
      </dl>
    )
  }
  if (isList(value)) {
    if (value.length === 0) return <span className="detail-none">[]</span>
    return (
      <ol className={styles.list}>
        {value.map((item, index) => (
          <li key={index}>
            <CellTree value={item} />
          </li>
        ))}
      </ol>
    )
  }
  if (value === null || value === undefined) return <span className="detail-none">null</span>
  const cid = asCid(value)
  if (cid) return <EntityLink id={new EntityID(cid.toString())} />
  if (isBytes(value)) return <Bytes value={value} />
  if (typeof value === 'string') return <span className={styles.string}>{value}</span>
  return <span className={styles.scalar}>{String(value)}</span>
}

function Bytes({ value }: { value: Uint8Array }) {
  return (
    <span className={styles.bytes}>
      {value.length} bytes · {toHex(value)}
    </span>
  )
}

/** Every column of one row, laid out key columns first. */
export function FieldList({ columns, row }: { columns: Column[]; row: TableRow }) {
  return (
    <dl className="kv kv-stacked">
      {columns.map((col) => (
        <div key={col.key} className={`kv-pair kv-${col.group}`}>
          <dt>
            {col.label}
            <span className="kv-schema">{formatSchema(col.schema)}</span>
          </dt>
          <dd>
            <ValueView value={row.cells[col.key] ?? null} />
          </dd>
        </div>
      ))}
    </dl>
  )
}

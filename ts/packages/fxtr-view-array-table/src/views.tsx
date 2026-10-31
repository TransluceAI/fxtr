// The renderers: an Array as a table (one column per dim, then one per struct field or one value
// column), and an inline link summarizing dims and row count. Cells render scalars inline, ids as
// EntityLinks, and nested lists / maps compactly.

import {
  EntityDataError,
  EntityLink,
  fieldOf,
  formatDims,
  formatSchema,
  isArrayData,
  isDataMap,
  isEntityID,
  rowsOf,
  structFields,
  unwrap,
  useEntity,
  type ArrayData,
  type DataValue,
  type EntityID,
  type LinkProps,
  type EntityPanelProps,
} from "fxtr-view";
import styles from "./array_table.module.css";

/** A data value in a cell: scalars, an id as a link, nested values compactly. */
export function Cell({ value }: { value: DataValue }) {
  if (isEntityID(value)) return <EntityLink id={value} />;
  if (value === null) return <span className={styles.null}>null</span>;
  switch (typeof value) {
    case "boolean":
    case "number":
    case "bigint":
      return <span className={styles.num}>{String(value)}</span>;
    case "string":
      return <span>{value}</span>;
  }
  if (value instanceof Uint8Array) return <span className={styles.bytes}>bytes[{value.length}]</span>;
  if (Array.isArray(value)) {
    return (
      <span>
        [
        {value.map((v, i) => (
          <span key={i}>
            {i > 0 ? ", " : ""}
            <Cell value={v} />
          </span>
        ))}
        ]
      </span>
    );
  }
  if (isDataMap(value)) {
    return (
      <span>
        {"{"}
        {Object.entries(value).map(([k, v], i) => (
          <span key={k}>
            {i > 0 ? ", " : ""}
            <span className={styles.field}>{k}</span>: <Cell value={v} />
          </span>
        ))}
        {"}"}
      </span>
    );
  }
  return <span>{String(value)}</span>;
}

/** An Array entity as a table. Registered in the `EntityPanel` slot for `ARRAY_TYPE`. Throws
 *  `EntityDataError` for Array-tagged data that is not Array-shaped. */
export function ArrayTablePanel({ id }: EntityPanelProps) {
  const array = unwrap(useEntity(id as EntityID<ArrayData>));
  if (!isArrayData(array)) throw new EntityDataError(`${id.cid} is not Array-shaped data`);
  const rows = rowsOf(array);
  const fields = structFields(array.schema);
  return (
    <div>
      <div className={styles.header}>
        <span className={styles.dims}>{formatDims(array.dims)}</span>{" "}
        <span className={styles.schema}>{formatSchema(array.schema)}</span>{" "}
        <span className={styles.count}>{rows.length} rows</span>
      </div>
      <table className={styles.table}>
        <thead>
          <tr>
            {array.dims.map(([name]) => (
              <th key={`dim:${name}`} className={styles.dimCol}>
                {name}
              </th>
            ))}
            {fields ? fields.map((f) => <th key={`field:${f}`}>{f}</th>) : <th>value</th>}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td colSpan={array.dims.length + (fields?.length ?? 1)} className={styles.empty}>
                empty
              </td>
            </tr>
          )}
          {rows.map((row, i) => (
            <tr key={i}>
              {row.key.map((k, j) => (
                <td key={j} className={styles.dimCol}>
                  <Cell value={k} />
                </td>
              ))}
              {fields ? (
                fields.map((f) => (
                  <td key={f}>
                    <Cell value={fieldOf(row.value, f)} />
                  </td>
                ))
              ) : (
                <td>
                  <Cell value={row.value} />
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** An Array entity as an inline link: `Array[prompt: int] · 12 rows`. */
export function ArrayInlineLink({ id }: LinkProps) {
  const result = useEntity(id as EntityID<ArrayData>);
  if (result.status !== "loaded" || !isArrayData(result.value)) {
    return <span className={styles.link}>Array</span>;
  }
  return (
    <span className={styles.link} title={id.cid}>
      Array{formatDims(result.value.dims)} · {result.value.rows.length} rows
    </span>
  );
}

// The wire shape of a fxtr Array entity (`fxtr/entity_defns/array.py`): `dims` as `[name, kind]`
// pairs, a `schema` wire tree, and `rows` as `[...keys, value]` lists sorted by key. This module
// types that shape and reads it; nothing here renders. It lives in fxtr-view (not in the array
// renderer package) because Array is the core value container: every step and workflow input and
// output is one, so any renderer of invocations needs the type.

import { isDataMap, type DataValue, type EntityData } from "./data";
import { EntityDataError } from "./errors";
import { isEntityID, type EntityID } from "./ids";

export const ARRAY_TYPE = "org.transluce.fxtr.core.Array";

export type DimKind = "int" | "str";
export type DimWire = readonly [name: string, kind: DimKind];

/** A schema's wire form: a leaf name, or a single-key map. */
export type SchemaWire =
  | "int"
  | "float"
  | "bool"
  | "str"
  | "bytes"
  | "id"
  | { readonly list: SchemaWire }
  | { readonly struct: { readonly [field: string]: SchemaWire } }
  | { readonly nullable: SchemaWire }
  | { readonly id: string };

export interface ArrayData extends EntityData {
  readonly $type: typeof ARRAY_TYPE;
  readonly dims: readonly DimWire[];
  readonly schema: SchemaWire;
  /** Each row: the key values in dim order, then the value. */
  readonly rows: readonly (readonly DataValue[])[];
}

/** A row split into its key and value. */
export interface ArrayRow {
  readonly key: readonly DataValue[];
  readonly value: DataValue;
}

/** Whether entity data has the Array shape (a structural check; the dims and rows are trusted). */
export function isArrayData(data: EntityData): data is ArrayData {
  return data.$type === ARRAY_TYPE && Array.isArray(data["dims"]) && Array.isArray(data["rows"]) && "schema" in data;
}

export function rowsOf(array: ArrayData): ArrayRow[] {
  const n = array.dims.length;
  return array.rows.map((row) => ({ key: row.slice(0, n), value: row[n] ?? null }));
}

/** The one value of a zero-dim array (a scalar input or output). Throws `EntityDataError` for an
 *  array with dims or without exactly one row. */
export function scalarValue(array: ArrayData): DataValue {
  if (array.dims.length !== 0 || array.rows.length !== 1) {
    throw new EntityDataError(
      `expected a scalar array, got ${formatDims(array.dims)} with ${array.rows.length} row(s)`,
    );
  }
  return array.rows[0]?.[0] ?? null;
}

/** The one value of a zero-dim array when it is an entity id (a scalar `id` input or output).
 *  Throws `EntityDataError` otherwise. */
export function scalarEntityId(array: ArrayData): EntityID {
  const value = scalarValue(array);
  if (!isEntityID(value)) throw new EntityDataError(`expected a scalar id array, got a ${typeof value} value`);
  return value;
}

/** The struct fields when the value schema is a struct (possibly nullable), else null. */
export function structFields(schema: SchemaWire): readonly string[] | null {
  if (typeof schema === "string") return null;
  if ("nullable" in schema) return structFields(schema.nullable);
  if ("struct" in schema) return Object.keys(schema.struct);
  return null;
}

/** A schema in the Python `str(schema)` spirit: `int`, `list[str]`, `{a: int, b: str}`, `id[T]`,
 *  `T | None`. */
export function formatSchema(schema: SchemaWire): string {
  if (typeof schema === "string") return schema;
  if ("list" in schema) return `list[${formatSchema(schema.list)}]`;
  if ("nullable" in schema) return `${formatSchema(schema.nullable)} | None`;
  if ("struct" in schema) {
    return `{${Object.entries(schema.struct)
      .map(([k, v]) => `${k}: ${formatSchema(v)}`)
      .join(", ")}}`;
  }
  return `id[${schema.id}]`;
}

/** `[prompt: int, sample: str]`. */
export function formatDims(dims: readonly DimWire[]): string {
  return `[${dims.map(([name, kind]) => `${name}: ${kind}`).join(", ")}]`;
}

/** The struct field of a row's value, when the value is a map. */
export function fieldOf(value: DataValue, field: string): DataValue {
  return isDataMap(value) ? (value[field] ?? null) : null;
}

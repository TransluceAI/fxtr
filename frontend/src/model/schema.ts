import { shortName } from 'fxtr-view-cbor-tree'
import type { ArrayTypeJson, Column, SchemaJson } from '../types'

/** The schema with any nullable wrapper removed, and whether there was one. */
export function unwrapNullable(schema: SchemaJson): { inner: SchemaJson; nullable: boolean } {
  if (typeof schema === 'object' && 'nullable' in schema) return { inner: schema.nullable, nullable: true }
  return { inner: schema, nullable: false }
}

/** The struct's fields when the schema (nullable or not) is a struct, else null. */
export function structFields(schema: SchemaJson): Record<string, SchemaJson> | null {
  const { inner } = unwrapNullable(schema)
  return typeof inner === 'object' && 'struct' in inner ? inner.struct : null
}

/** The schema of the value at `path` in a value of `schema`: struct fields by name, list items by
 *  index. Undefined for a path the schema does not have; null for one that goes on through an
 *  entity reference, whose own fields the schema does not describe. */
export function schemaAt(schema: SchemaJson, path: readonly string[]): SchemaJson | null | undefined {
  let current = schema
  for (const name of path) {
    const { inner } = unwrapNullable(current)
    if (isReference(inner)) return null
    if (typeof inner === 'object' && 'struct' in inner) {
      const field = inner.struct[name]
      if (field === undefined) return undefined
      current = field
    } else if (typeof inner === 'object' && 'list' in inner) {
      if (!/^\d+$/.test(name)) return undefined
      current = inner.list
    } else return undefined
  }
  return current
}

/** Whether a schema (nullable or not) is an entity reference: typed (`{id}`) or not (`id`). */
export function isReference(schema: SchemaJson): boolean {
  const { inner } = unwrapNullable(schema)
  return inner === 'id' || (typeof inner === 'object' && 'id' in inner)
}

/** Whether values of the schema sort and align as numbers. */
export function isNumeric(schema: SchemaJson): boolean {
  const { inner } = unwrapNullable(schema)
  return inner === 'int' || inner === 'float'
}

/** A schema in the notation `str`, `int?`, `list[struct{a: int, b: str}]`, `id[org.example.Config]`. */
export function formatSchema(schema: SchemaJson): string {
  if (typeof schema === 'string') return schema
  if ('id' in schema) return `id[${schema.id}]`
  if ('list' in schema) return `list[${formatSchema(schema.list)}]`
  if ('nullable' in schema) return `${formatSchema(schema.nullable)}?`
  const fields = Object.entries(schema.struct).map(([name, s]) => `${name}: ${formatSchema(s)}`)
  return `struct{${fields.join(', ')}}`
}

/** An array type in the notation `[model: str, sample: int]: struct{...}`. */
export function formatArrayType(type: ArrayTypeJson): string {
  const dims = type.dims.map(([name, kind]) => `${name}: ${kind}`).join(', ')
  return `[${dims}]: ${formatSchema(type.schema)}`
}

/** A type in full: `formatArrayType`, or just `formatSchema` when it has no dims (a single value). */
export function formatType(type: ArrayTypeJson): string {
  return type.dims.length ? formatArrayType(type) : formatSchema(type.schema)
}

/**
 * A type at a glance: `struct` without its fields, an entity by its short type name (`Report`),
 * `entity` for an untyped id, and `Array[...]` of those, without its dims, when it has any.
 */
export function typeLabel(type: ArrayTypeJson): string {
  return type.dims.length ? `Array[${schemaLabel(type.schema)}]` : schemaLabel(type.schema)
}

/** A schema at a glance, as `typeLabel` writes one. */
export function schemaLabel(schema: SchemaJson): string {
  if (schema === 'id') return 'entity'
  if (typeof schema === 'string') return schema
  if ('id' in schema) return shortName(schema.id)
  if ('list' in schema) return `list[${schemaLabel(schema.list)}]`
  if ('nullable' in schema) return `${schemaLabel(schema.nullable)}?`
  return 'struct'
}

/** What one call of a node's function returns: its output type without the dims the run added,
 *  which are its mapped dims and replica dims. */
export function returnType(type: ArrayTypeJson, runDims: readonly string[]): ArrayTypeJson {
  const names = new Set(type.dims.map(([name]) => name))
  const missing = runDims.filter((dim) => !names.has(dim))
  if (missing.length) throw new Error(`run dims ${missing.join(', ')} are not dims of the output type`)
  return { dims: type.dims.filter(([name]) => !runDims.includes(name)), schema: type.schema }
}

export const keyColumnKey = (dim: string) => `key:${dim}`
export const valueColumnKey = (field: string | null) => `value:${field ?? ''}`

/**
 * The columns of an array type: one key column per dim, then one value column per struct field,
 * or a single anonymous `value` column when the value is not a struct.
 */
export function columnsFor(type: ArrayTypeJson): Column[] {
  const keys: Column[] = type.dims.map(([name, kind]) => ({
    key: keyColumnKey(name),
    label: name,
    group: 'key',
    schema: kind,
    numeric: kind === 'int',
  }))
  const fields = structFields(type.schema)
  const values: Column[] = fields
    ? Object.entries(fields).map(([name, schema]) => ({
        key: valueColumnKey(name),
        label: name,
        group: 'value',
        schema,
        numeric: isNumeric(schema),
      }))
    : [
        {
          key: valueColumnKey(null),
          label: 'value',
          group: 'value',
          schema: type.schema,
          numeric: isNumeric(type.schema),
        },
      ]
  return [...keys, ...values]
}

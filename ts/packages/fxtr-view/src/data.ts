// Entity data as a renderer receives it: the decoded DRISL value tree, with every reference leaf
// already an `EntityID`. An entity's data is always a map carrying the `$type` tag; the tag is the
// whole of what the library knows about a type. Renderers narrow `EntityData` to their own type's
// shape themselves.

import { EntityDataError } from "./errors";
import { EntityID } from "./ids";

/** The reserved key holding an entity's type name. */
export const TYPE_KEY = "$type";

/** A decoded data value. Large integers decode as `bigint`; bytes as `Uint8Array`. */
export type DataValue =
  | null
  | boolean
  | number
  | bigint
  | string
  | Uint8Array
  | EntityID
  | readonly DataValue[]
  | DataMap;

/** A decoded map. */
export interface DataMap {
  readonly [key: string]: DataValue;
}

/** An entity's decoded data: a map with a `$type` tag. */
export type EntityData = DataMap & { readonly [TYPE_KEY]: string };

/** Whether a value is a decoded map (not an array, bytes, or an id). */
export function isDataMap(value: unknown): value is DataMap {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    !(value instanceof Uint8Array) &&
    !(value instanceof EntityID)
  );
}

/** Whether a value is entity data: a map whose `$type` is a non-empty string. */
export function isEntityData(value: unknown): value is EntityData {
  return isDataMap(value) && typeof value[TYPE_KEY] === "string" && value[TYPE_KEY].length > 0;
}

/** The entity's type name (its `$type` tag). Throws `EntityDataError` if the tag is missing or
 *  not a non-empty string. */
export function typeNameOf(data: EntityData): string {
  const tag: unknown = data[TYPE_KEY];
  if (typeof tag !== "string" || tag.length === 0) {
    throw new EntityDataError(`entity data has no ${TYPE_KEY} tag`);
  }
  return tag;
}

/** The maps of a value a host hands a `PartialEntityPanel` or `PartialEntityInline` renderer that
 *  are still being produced (see `markPartial`). A weak set, so marking keeps nothing alive and
 *  adds nothing to the data. */
const partialParts = new WeakSet<object>();

/** For a host: mark a map of a value still being produced as itself still being produced. A host
 *  unwrapping a partial value's wire form marks each map that came out of a partial wrapper, so a
 *  renderer can tell which parts are finished (`isPartial`). Returns the map. */
export function markPartial<T extends DataMap>(map: T): T {
  partialParts.add(map);
  return map;
}

/** Whether a part of a value handed to a `PartialEntityPanel` or `PartialEntityInline` renderer (the
 *  value itself, or a map inside it) is still being produced: its fields not yet known are null,
 *  and it may still grow. False for a finished part, and for anything not a map. Read it on the
 *  value as handed over: a copy of a map is not marked. */
export function isPartial(value: DataValue | undefined): boolean {
  return typeof value === "object" && value !== null && partialParts.has(value);
}

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

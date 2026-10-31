// Entity ids as the frontend sees them: a content id (the cid string the backend prints).
//
// `EntityID` is a class so an id is never confused with a string or a data map that happens to
// have a `cid` field (`isEntityID` is an `instanceof` check; there is one fxtr-view instance per
// module graph). Ids that cross a JSON boundary (drag payloads, persisted panel specs) go through
// `idToJson` / `idFromJson` explicitly, since a parsed object is not an instance.

import type { EntityData } from "./data";
import { EntityDataError } from "./errors";

/** Plain JSON data. */
export type JsonValue =
  | null
  | boolean
  | number
  | string
  | readonly JsonValue[]
  | { readonly [key: string]: JsonValue };

/** The JSON form of an id, what `idToJson` produces and `idFromJson` accepts. */
export interface EntityIDJson {
  readonly cid: string;
}

/**
 * An entity id. `T` is a phantom: the TypeScript type the entity's decoded data is asserted to
 * have (`useEntity(id)` returns a `LoadResult<T>`). Nothing checks it at runtime; a renderer for
 * a type makes the assertion once, when it casts the untyped id dispatch handed it, and the
 * ids inside its typed data are then already typed.
 */
export class EntityID<T extends EntityData = EntityData> {
  /** Phantom (covariant: a typed id is usable as an untyped one); never assigned. `declare` so
   *  no field is emitted. */
  declare readonly __data?: T;

  constructor(readonly cid: string) {
    if (typeof cid !== "string" || cid.length === 0) {
      throw new EntityDataError("an EntityID needs a non-empty cid string");
    }
    Object.freeze(this);
  }

  /** Same cid. */
  equals(other: EntityID): boolean {
    return this.cid === other.cid;
  }

  /** Lets `JSON.stringify` produce the `EntityIDJson` form. Read it back with `idFromJson`. */
  toJSON(): EntityIDJson {
    return idToJson(this);
  }

  toString(): string {
    return this.cid;
  }
}

/** Whether a value is an `EntityID`. */
export function isEntityID(value: unknown): value is EntityID {
  return value instanceof EntityID;
}

/** The JSON form of an id. */
export function idToJson(id: EntityID): EntityIDJson {
  return { cid: id.cid };
}

/** An id from its JSON form (a parsed drag payload, a persisted spec). Throws `EntityDataError`
 *  when the value is not shaped like one. */
export function idFromJson(value: unknown): EntityID {
  if (value instanceof EntityID) return value;
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new EntityDataError("an entity id's JSON form is an object with a cid");
  }
  const { cid } = value as { cid?: unknown };
  if (typeof cid !== "string" || cid.length === 0) {
    throw new EntityDataError("an entity id's JSON form needs a non-empty cid string");
  }
  return new EntityID(cid);
}

/** The prefix every cid in the system shares (CIDv1, base32, dag-cbor, sha-256), carrying no
 *  information a reader needs. */
export const COMMON_CID_PREFIX = "bafyrei";

/** How many characters of a cid (after the common prefix) the short form keeps. */
export const SHORT_CID_LENGTH = 8;

/** A cid split for display: the common prefix (empty if absent), the short run, and the rest.
 *  `prefix + short + rest` is the cid. */
export interface SplitCid {
  readonly prefix: string;
  readonly short: string;
  readonly rest: string;
}

export function splitCid(cid: string, length = SHORT_CID_LENGTH): SplitCid {
  const prefix = cid.startsWith(COMMON_CID_PREFIX) ? COMMON_CID_PREFIX : "";
  const body = cid.slice(prefix.length);
  return { prefix, short: body.slice(0, length), rest: body.slice(length) };
}

/** A cid for display, git style: the common prefix dropped and the first `length` characters
 *  kept, no ellipsis. Always a literal substring of the cid, the one `CidText` emphasizes. */
export function shortCid(cid: string, length = SHORT_CID_LENGTH): string {
  return splitCid(cid, length).short;
}

/** A stable string key for an id: its cid. Use it for React keys, caches, and panel
 *  identity. */
export function idKey(id: EntityID): string {
  return id.cid;
}

// Pure atom constructors for the state conventions. fxtr-view holds no store: these derive atoms
// from atoms the caller owns.
//
// - `subAtom` focuses a record atom on one key (a panel carving child slots off its storage).
// - `validatedAtom` lifts a JSON storage atom to a typed one guarded by a validity check, so a
//   stale or foreign shape resets to the initial value instead of crashing.

import { atom, type PrimitiveAtom, type SetStateAction } from "jotai";
import type { JsonValue } from "./ids";

function apply<T>(update: SetStateAction<T>, prev: T): T {
  return typeof update === "function" ? (update as (prev: T) => T)(prev) : update;
}

/** A writable atom on one key of a record atom. Writes spread the parent, so the parent must
 *  hold a plain record (not an array). */
export function subAtom<T extends object, K extends keyof T>(parent: PrimitiveAtom<T>, key: K): PrimitiveAtom<T[K]> {
  return atom(
    (get) => get(parent)[key],
    (get, set, update: SetStateAction<T[K]>) => {
      const prev = get(parent);
      set(parent, { ...prev, [key]: apply(update, prev[key]) });
    },
  );
}

/** `T` if every part of it is JSON data, else `never`. Unlike `T extends JsonValue`, this accepts
 *  types declared with `interface` (which have no implicit index signature). */
export type JsonCompatible<T> = T extends null | boolean | number | string
  ? T
  : T extends readonly (infer U)[]
    ? readonly JsonCompatible<U>[]
    : // eslint-disable-next-line @typescript-eslint/no-explicit-any
      T extends (...args: any[]) => unknown
      ? never
      : T extends object
        ? { readonly [K in keyof T]: JsonCompatible<T[K]> }
        : never;

/** A typed view of a JSON storage atom: reads yield the stored value when `isValid` accepts it,
 *  else `initial`; function-form writes see that same value. Typical use: a panel narrows its
 *  `StorageAtom` to its own versioned state shape, `isValid` checking a version tag. */
export function validatedAtom<T>(
  parent: PrimitiveAtom<JsonValue>,
  initial: T & JsonCompatible<T>, // `T` is inferred from `initial`; the intersection rejects non-JSON parts
  isValid: (value: unknown) => value is T,
): PrimitiveAtom<T> {
  const current = (value: JsonValue): T => (isValid(value) ? value : initial);
  return atom(
    (get) => current(get(parent)),
    (get, set, update: SetStateAction<T>) => {
      // T is JSON-compatible (checked at `initial`); the cast bridges the interface/index-signature gap.
      set(parent, apply(update, current(get(parent))) as unknown as JsonValue);
    },
  );
}

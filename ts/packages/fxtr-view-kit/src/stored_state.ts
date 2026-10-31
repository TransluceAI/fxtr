// A panel's UI state kept in the storage the host hands it (`StorageAtom`), so a pick, a toggle,
// or a sort survives the panel remounting (a pane reopened, the view switched away and back):
// `useStoredState` is `useState` over one key of that storage. An optional type guard validates
// stored values when a view needs to reject stale state from a different schema or set of choices.

import { useMemo } from "react";
import { atom, useAtom, type SetStateAction } from "jotai";
import type { JsonValue, StorageAtom } from "fxtr-view";

type JsonRecord = { readonly [key: string]: JsonValue };

function isRecord(value: JsonValue | undefined): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** `useState` kept under `key` in the panel's `storage`. Stored values are trusted as `T` unless
 *  `isValid` is given; a missing or rejected value reads as `initial`. A type's possible values
 *  cannot be inferred from its initial value (which may be null, or one member of a union).
 *  `initial` and `isValid` are captured once per storage and key; several keys share one storage. */
export function useStoredState<T extends JsonValue>(
  storage: StorageAtom,
  key: string,
  initial: T,
  isValid?: (value: JsonValue) => value is T,
): [T, (update: SetStateAction<T>) => void] {
  // The initial value and validator are left out of the dependencies: captured once, as documented.
  const stored = useMemo(() => {
    const current = (parent: JsonValue): T => {
      const value = isRecord(parent) ? parent[key] : undefined;
      return value !== undefined && (isValid === undefined || isValid(value)) ? (value as T) : initial;
    };
    return atom(
      (get) => current(get(storage)),
      (get, set, update: SetStateAction<T>) => {
        const parent = get(storage);
        const next = typeof update === "function" ? (update as (prev: T) => T)(current(parent)) : update;
        set(storage, { ...(isRecord(parent) ? parent : {}), [key]: next });
      },
    );
  }, [storage, key]);
  return useAtom(stored);
}

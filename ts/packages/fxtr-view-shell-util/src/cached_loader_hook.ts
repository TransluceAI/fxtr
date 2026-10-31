// `LoadResult` hooks over a plain async loader, sharing one load per key: the first reader of a
// key starts the load, every later reader (in the same jotai store) sees the same atom, and an
// already loaded value renders without a loading tick. A pending result carries the atom's
// promise (so `unwrap` can suspend on it). Nothing is ever evicted; a shell that wants to flush
// remounts its jotai Provider. `createCachedLoaderHook` is the general form; the entity and
// invocation hooks a `ViewShell` needs are its two specializations.

import { atom, useAtomValue, useStore, type Atom } from "jotai";
import { loadable } from "jotai/utils";
import {
  idKey,
  invocationRefKey,
  type EntityData,
  type EntityID,
  type InvocationRecord,
  type InvocationRef,
  type LoadResult,
} from "fxtr-view";

export type EntityLoader = (id: EntityID) => Promise<EntityData>;
export type InvocationLoader = (ref: InvocationRef) => Promise<InvocationRecord>;

/** Build a `LoadResult` hook from a loader and the identity of its keys. The returned function is
 *  a hook; keep one instance per shell for the provider's lifetime. */
export function createCachedLoaderHook<K, V>(
  keyOf: (key: K) => string,
  load: (key: K) => Promise<V>,
  debugPrefix: string,
): (key: K) => LoadResult<V> {
  const atoms = new Map<string, Atom<Promise<V>>>();
  const atomFor = (key: K): Atom<Promise<V>> => {
    const id = keyOf(key);
    let found = atoms.get(id);
    if (found === undefined) {
      found = atom(async () => load(key));
      found.debugLabel = `${debugPrefix}:${id}`;
      atoms.set(id, found);
    }
    return found;
  };
  return function useCachedValue(key: K): LoadResult<V> {
    const valueAtom = atomFor(key);
    const store = useStore();
    const state = useAtomValue(loadable(valueAtom));
    switch (state.state) {
      case "loading":
        // The atom's value is the load's promise; settle-or-reject either way ends the pending state.
        return { status: "loading", promise: store.get(valueAtom).catch(() => undefined) };
      case "hasData":
        return { status: "loaded", value: state.data };
      case "hasError":
        return { status: "error", error: state.error };
    }
  };
}

/** A `ViewShell.useEntity` over an entity loader, one load per id key. */
export function createCachedEntityHook(load: EntityLoader): (id: EntityID) => LoadResult<EntityData> {
  return createCachedLoaderHook(idKey, load, "entity");
}

/** A `ViewShell.useInvocation` over an invocation loader, one load per ref key. */
export function createCachedInvocationHook(load: InvocationLoader): (ref: InvocationRef) => LoadResult<InvocationRecord> {
  return createCachedLoaderHook(invocationRefKey, load, "invocation");
}

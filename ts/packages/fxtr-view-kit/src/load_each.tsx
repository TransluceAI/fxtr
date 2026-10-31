// Loading many entities at once, for a view of several (a merge, an aggregate over a step's
// outputs). `LoadEach` starts every load together and hands its body the values in hand, counting
// the rest, rather than suspending until the last one arrives, so a view can fill in as its data
// loads and as a running job produces it.
//
// The ids may change from render to render (a step panel's spawns gain outputs while the step
// runs), and there may be any number of them, so each distinct id is loaded by a component of its
// own, keyed by the id, which reports its value up once loaded: the number of hooks any one
// component calls stays fixed, and the body stays mounted throughout, keeping its own state.

import { useCallback, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { idKey, useEntity, type EntityData, type EntityID } from "fxtr-view";

/**
 * `children` rendered with every distinct id's parsed value in hand (by `idKey`) and how many
 * distinct ids are still loading. `parse` must be stable across renders (a module-level function,
 * or memoized). A failed load or parse throws into the host's error boundary.
 */
export function LoadEach<T>({
  ids,
  parse,
  children,
}: {
  ids: readonly EntityID[];
  parse: (data: EntityData) => T;
  children: (loaded: ReadonlyMap<string, T>, pending: number) => ReactNode;
}) {
  const values = useRef(new Map<string, T>());
  const [, setVersion] = useState(0);
  const onLoad = useCallback((key: string, value: T) => {
    values.current.set(key, value);
    setVersion((v) => v + 1);
  }, []);
  const distinct = new Map<string, EntityID>();
  for (const id of ids) distinct.set(idKey(id), id);
  const loaded = new Map<string, T>();
  for (const key of distinct.keys()) {
    const value = values.current.get(key);
    if (value !== undefined) loaded.set(key, value);
  }
  return (
    <>
      {[...distinct].map(([key, id]) => (
        <Load key={key} id={id} idKey={key} parse={parse} onLoad={onLoad} />
      ))}
      {children(loaded, distinct.size - loaded.size)}
    </>
  );
}

function Load<T>({
  id,
  idKey: key,
  parse,
  onLoad,
}: {
  id: EntityID;
  idKey: string;
  parse: (data: EntityData) => T;
  onLoad: (key: string, value: T) => void;
}) {
  const result = useEntity(id);
  if (result.status === "error") throw result.error;
  const data = result.status === "loaded" ? result.value : null;
  const value = useMemo(() => (data === null ? null : { parsed: parse(data) }), [data, parse]);
  // Before paint, so values already in the shell's cache show on the first frame, not a count.
  useLayoutEffect(() => {
    if (value !== null) onLoad(key, value.parsed);
  }, [key, value, onLoad]);
  return null;
}

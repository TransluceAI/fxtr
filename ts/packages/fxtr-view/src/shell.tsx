// The one seam between renderer code and whatever hosts it: the `ViewShell` context. A shell
// provides the value; fxtr-view never implements any member, and every user-facing hook or
// component here is a thin forwarder to the corresponding member. The default value faults loudly
// on use, so a missing provider is a visible mistake rather than a silent blank.

import { createContext, useContext, type ComponentType, type ReactNode } from "react";
import type { EntityData } from "./data";
import { ShellFault } from "./errors";
import type { EntityID } from "./ids";
import type { InvocationRecord, InvocationRef } from "./invocation";
import type { EntityLinkImplProps, EntityLinks, ViewLink, ViewLinkSpec, ViewParams } from "./link";

/** The state of a load. A pending result carries the promise of its completion (settling, either
 *  way, once the load is no longer pending), which is what lets `unwrap` suspend on it. */
export type LoadResult<T> =
  | { readonly status: "loading"; readonly promise: Promise<unknown> }
  | { readonly status: "loaded"; readonly value: T }
  | { readonly status: "error"; readonly error: unknown };

/** The loaded value, or suspend: throws the pending promise while loading (React Suspense; a host
 *  renders every renderer under a Suspense boundary) and throws the error on failure (caught by
 *  the host's error boundary around the renderer). Not a hook: a plain function over a result, `unwrap(useEntity(id))`, for a renderer that cannot show anything
 *  without the data. Use the result directly to render something partial while it loads. */
export function unwrap<T>(result: LoadResult<T>): T {
  switch (result.status) {
    case "loaded":
      return result.value;
    case "loading":
      throw result.promise;
    case "error":
      throw result.error;
  }
}

/** The context value a shell provides. */
export interface ViewShell {
  /** A hook implementation: the entity's data. Must obey the rules of hooks: a stable function
   *  for the provider's lifetime, calling the same hooks in the same order per render. Caching and
   *  sharing across readers are the shell's decisions. */
  readonly useEntity: (id: EntityID) => LoadResult<EntityData>;
  /** A hook implementation: the record of a step or workflow invocation, under the same rules as
   *  `useEntity`. Optional: a shell with no invocation data (one that shows entities alone) leaves
   *  it out, and every `useInvocation` below it yields an error result saying so. */
  readonly useInvocation?: (ref: InvocationRef) => LoadResult<InvocationRecord>;
  /** A hook implementation: the dim keys known where the view is shown (`ViewKeys`), under the
   *  same rules as `useEntity`. Optional: a shell that slices nothing leaves it out, and every
   *  `useKeys` below it yields no keys. */
  readonly useKeys?: () => ViewKeys;
  /** A hook implementation: a link to what `spec` names from the view drawing it (`useLink`; null:
   *  no link), or null where none can be opened, under the same rules as `useEntity`. Optional: a
   *  shell that opens no views leaves it out, and every `useLink` below it yields null. */
  readonly useLink?: (spec: ViewLinkSpec | null, params: ViewParams) => ViewLink | null;
  /** The shell's implementation of `EntityLink`: the entity's inline link view, as the shell
   *  chooses it, with the shell's link behavior (click, drag, context menu). Navigation stays
   *  inside the shell: renderers link, they do not navigate. */
  readonly EntityLinkImpl: ComponentType<EntityLinkImplProps>;
  /** A hook implementation, under the same rules as `useEntity`: following a link from an element
   *  a renderer draws itself, with the behavior of `EntityLinkImpl`'s click, and whether the shell
   *  shows the entity now. Optional: under a shell that leaves it out, nothing opens. */
  readonly useEntityLinks?: () => EntityLinks;
}

function missingProvider(member: string): never {
  throw new ShellFault(`fxtr-view: ${member} used outside a ViewShellProvider`);
}

/** The default value: every member faults on use. */
export const MISSING_SHELL: ViewShell = Object.freeze({
  useEntity: () => missingProvider("useEntity"),
  EntityLinkImpl: () => missingProvider("EntityLinkImpl"),
});

export const ViewShellContext = createContext<ViewShell>(MISSING_SHELL);

/** Provide a shell to the renderers below. */
export function ViewShellProvider({ shell, children }: { shell: ViewShell; children?: ReactNode }) {
  return <ViewShellContext.Provider value={shell}>{children}</ViewShellContext.Provider>;
}

/** The current shell. */
export function useViewShell(): ViewShell {
  return useContext(ViewShellContext);
}

/** The entity's data, through the shell. `T` is the id's phantom data type; the cast is the
 *  assertion the id already made. */
export function useEntity<T extends EntityData>(id: EntityID<T>): LoadResult<T> {
  const shell = useViewShell();
  return shell.useEntity(id) as LoadResult<T>;
}

/**
 * The dim keys known where a view is shown. `slice` is what the data it was handed is filtered on.
 * `selected` is what the views opened from this one show on dims it is not filtered on, several
 * values per dim when several are open: the view is handed every value of those dims, and can show
 * the open ones highlighted among the rest.
 */
export interface ViewKeys {
  readonly slice: Readonly<Record<string, string | number>>;
  readonly selected: Readonly<Record<string, readonly (string | number)[]>>;
}

const NO_KEYS: ViewKeys = Object.freeze({ slice: Object.freeze({}), selected: Object.freeze({}) });

/** The dim keys known where the view is shown, through the shell. */
export function useKeys(): ViewKeys {
  const shell = useViewShell();
  return shell.useKeys === undefined ? NO_KEYS : shell.useKeys();
}

/** The result every `useInvocation` yields under a shell that serves no invocations. */
const NO_INVOCATIONS: LoadResult<InvocationRecord> = Object.freeze({
  status: "error",
  error: new ShellFault("this shell serves no invocations"),
});

/** An invocation's record, through the shell. A shell's members are stable for the provider's
 *  lifetime, so whether the shell's hook is called is fixed per mount and the rules of hooks hold. */
export function useInvocation(ref: InvocationRef): LoadResult<InvocationRecord> {
  const shell = useViewShell();
  return shell.useInvocation === undefined ? NO_INVOCATIONS : shell.useInvocation(ref);
}

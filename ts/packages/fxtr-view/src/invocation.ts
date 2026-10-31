// Invocations as renderer code sees them: a reference naming one step or workflow invocation of a
// job; a spawn, which is such a reference with its key within a run and its output (what a host
// hands a renderer, and what a record lists under its children); and the record a shell serves for
// a reference (through `ViewShell.useInvocation`). The record is a projection, not a backend's wire
// shape: the registered function name the renderer was keyed on, the input arrays by argument name,
// the output array, and the runs the body made, each with its spawns, so a renderer can walk its
// subtree by asking for a spawn's record in turn. Every array is an `EntityID`, loaded lazily with
// `useEntity` like any other reference.

import type { ArrayData } from "./array_data";
import type { EntityID } from "./ids";

/** One invocation: a workflow invocation by pathname, or a step by cache key, within a job. */
export type InvocationRef =
  | { readonly jobId: string; readonly kind: "workflow"; readonly pathname: string }
  | { readonly jobId: string; readonly kind: "step"; readonly cacheKey: string };

/** The identity of a ref for caches and React keys. Pathnames and cache keys contain `/`, `[`,
 *  `:`, `,` and quoted JSON, so the key is a JSON-encoded tuple rather than a joined string. */
export function invocationRefKey(ref: InvocationRef): string {
  return JSON.stringify(
    ref.kind === "workflow" ? [ref.jobId, "workflow", ref.pathname] : [ref.jobId, "step", ref.cacheKey],
  );
}

/** Why an invocation failed, as the runner recorded it. */
export interface InvocationError {
  readonly typeName: string;
  readonly message: string;
}

/** One invocation with its key within a run and what it produced. */
export interface Spawn {
  /** The mapped and replica dim values by dim name; `{}` for an unmapped run, or for an
   *  invocation not shown as one of a run's spawns (a workflow level on its own). */
  readonly key: Readonly<Record<string, string | number>>;
  readonly ref: InvocationRef;
  /** The array the invocation produced; null until it has one. */
  readonly output: EntityID<ArrayData> | null;
}

/** One `run_step` / `run_child_workflow` node of a workflow invocation. */
export interface ChildRun {
  /** The local node name (the first argument of `ctx.run_step` / `ctx.run_child_workflow`). */
  readonly nodeName: string;
  /** The registered `@step` / `@workflow` name the run calls. */
  readonly name: string;
  readonly kind: "workflow" | "step";
  /** The dims of the spawns' keys in order: the mapped dims, then the replica dims. */
  readonly dims: readonly string[];
  readonly spawns: readonly Spawn[];
}

/** What a shell serves for one invocation. */
export interface InvocationRecord {
  readonly ref: InvocationRef;
  /** The registered `@step` / `@workflow` name: what renderers are keyed on. */
  readonly name: string;
  /** The runner's state, as the backend spells it. */
  readonly state: string;
  readonly error: InvocationError | null;
  /** The input arrays by argument name. */
  readonly inputs: Readonly<Record<string, EntityID<ArrayData>>>;
  /** The result array; null until the invocation has one. */
  readonly output: EntityID<ArrayData> | null;
  /** The runs the body made, in emission order. Empty for a step. */
  readonly children: readonly ChildRun[];
}

/**
 * Types describing a single dispatch-key registry entry.
 *
 * A registry entry binds a `(DispatchKey, RefKind)` pair to:
 *
 *   - an *effect-fn factory* that, given the per-entry atoms,
 *     returns the `(get, set) => Cleanup | void` body that drives
 *     the resolution state machine forward;
 *   - the initial value of the state-machine state-atom;
 *   - the fallback component (initial value of the per-entry
 *     `componentAtom`);
 *   - the error-boundary component (rendered when the resolved
 *     component throws during render).
 *
 * The runtime that mounts the effect-fn into the React tree
 * lives in `dispatch_provider.tsx` (not yet implemented); this
 * module is types only.
 */

import type { EntityRefStub, FxtrSessionAtom, ServiceRefStub } from "@fxtr-ui/core";
import type { Getter, Setter, WritableAtom } from "jotai";
import type { ComponentType } from "react";

import type { EntityKeyFallbacks, ServiceKeyFallbacks } from "./fallbacks";

/**
 * The shape of a *resolved* component for a given (Stub, Props)
 * pair. Both arms of the dispatch (entity / service) write
 * components of this shape into the per-entry `componentAtom`.
 *
 * `Stub` is the concrete ref-stub kind (`EntityRefStub` or
 * `ServiceRefStub`, narrowed by the registry's route). `Props`
 * is the dispatch key's extra-props type — everything beyond
 * `stub`.
 */
export type DispatchedComponent<Stub, Props extends object> = ComponentType<
	{ readonly stub: Stub } & Props
>;

/**
 * The shape of the *fallback* component rendered when the
 * dispatched component throws during render. The internal
 * React error boundary (`DispatchErrorBoundary`) is what does
 * the catching; this component is the one it renders in place
 * of the failed subtree.
 *
 * The static dispatch helpers also use this fallback to
 * surface terminal `errored` state-machine phases — their
 * synthetic `ErrorThrower` throws during render, the
 * boundary catches, and the consumer-supplied
 * `ErrorFallback` renders the error message.
 *
 * Receives the same `stub` + dispatch-key `Props` as the
 * resolved component, plus the caught `error` value.
 */
export type ErrorFallbackComponent<Stub, Props extends object> = ComponentType<
	{ readonly stub: Stub; readonly error: unknown } & Props
>;

/**
 * A writable jotai atom holding the *current rendered
 * component* for a dispatch entry. Initialized to the entry's
 * `Fallback` and overwritten by the effect-fn as the state
 * machine progresses. Wrapped by the provider so writes that
 * change the component value also reset the entry's shared sub-
 * atom to `null` in the same flush.
 */
export type ComponentAtom<Stub, Props extends object> = WritableAtom<
	DispatchedComponent<Stub, Props>,
	[DispatchedComponent<Stub, Props>],
	void
>;

/**
 * Plain in-memory writable atom of a human-readable progress
 * string. Lives outside the dispatcher's session (transient UI
 * hint, not wire-persisted). Read by fallback components via
 * `useDispatchProgressMessage()`.
 */
export type ProgressMessageAtom = WritableAtom<string, [string], void>;

/**
 * The effect-fn factory: receives the per-entry atoms, returns
 * the actual `(get, set) => Cleanup | void` effect body that
 * `jotai-effect` will mount and run.
 *
 * The body should:
 *   - `get(stateAtom)` to read the current state-machine state
 *     (subscribing to changes);
 *   - `set(stateAtom, ...)` to advance the machine. Function-
 *     form writes get a `FxtrSessionUpdateCtx` so they can
 *     `ctx.allocateLoad(stub)` / `ctx.allocateGetEntityMetadata`
 *     / `ctx.allocateGetServiceMetadata` inline;
 *   - `set(componentAtom, NewComponent)` to publish the
 *     resolved (or transitional) component to consumers;
 *   - `set(progressMessageAtom, "loading…")` to publish a
 *     human-readable status string;
 *   - return a cleanup function if it has any non-atom side
 *     effects to undo.
 *
 * `S` is the registry-entry author's choice of state-machine
 * state shape. Anything structurally `StateValue`-shaped works
 * (the wire codec is the runtime gate). The factory closes over
 * `Stub`, `Props`, and `S`; mismatches between an entry's
 * `effectFn` and its `initialState` are caught at the type
 * level.
 */
export type DispatchEffectFactory<Stub, Props extends object, S> = (params: {
	readonly stub: Stub;
	readonly stateAtom: FxtrSessionAtom<S>;
	readonly componentAtom: ComponentAtom<Stub, Props>;
	readonly progressMessageAtom: ProgressMessageAtom;
	/**
	 * Per-key fallback components, merged with any dispatcher-side
	 * overrides. Sourced from the `DispatchKey` plus the dispatcher's
	 * `overrides` option. The state machine reads `fallbacks.NoMatch`
	 * / `fallbacks.Builtin` etc. directly when transitioning to a
	 * terminal phase that needs to render one.
	 */
	readonly fallbacks: StubFallbacks<Stub, Props>;
	// biome-ignore lint/suspicious/noConfusingVoidType: matches jotai-effect's `(get, set) => void | (() => void)` cleanup-return convention so consumers can use plain `return;` to mean "no cleanup."
}) => (get: Getter, set: Setter) => void | (() => void);

/**
 * The fallback bundle as seen by a state machine. Entity-side
 * entries see `EntityKeyFallbacks<Props>` (with optional Builtin);
 * service-side entries see `ServiceKeyFallbacks<Props>` (no
 * Builtin). Both-allowed keys carry `AnyKeyFallbacks` on the key
 * itself; the dispatcher narrows them to one of the two concrete
 * shapes before calling `effectFn`, since each entry only ever
 * fires for one stub kind.
 */
export type StubFallbacks<Stub, Props extends object> = [Stub] extends [EntityRefStub]
	? EntityKeyFallbacks<Props>
	: [Stub] extends [ServiceRefStub]
		? ServiceKeyFallbacks<Props>
		: EntityKeyFallbacks<Props> | ServiceKeyFallbacks<Props>;

/**
 * Full registry-entry shape: effect-fn factory + initial state
 * + initial progress message.
 *
 * Constructed by helpers like `staticEntityTypeDispatch` or by
 * hand. Passed to `createDispatchRegistry` paired with a matching
 * `DispatchKey` via `entityEntry(key, entry)` / `serviceEntry(key,
 * entry)`.
 *
 * The fallback components used by the dispatcher (loading,
 * no-match, error, builtin) live on the `DispatchKey` itself
 * rather than the registry entry — see `keys.ts` and
 * `fallbacks.ts`. The state machine receives those via
 * `createBaseEntry` through the dispatcher's threading and writes
 * them through `componentAtom`.
 */
export interface DispatchKeyRegistryEntry<Stub, Props extends object, S = unknown> {
	readonly effectFn: DispatchEffectFactory<Stub, Props, S>;
	readonly initialState: S;
	readonly initialProgressMessage?: string;
}

/**
 * Convenience aliases for the two concrete `Stub` kinds. Used
 * by the registry's lookup methods and by the entry-helper
 * factories (`entityEntry` / `serviceEntry`).
 */
export type EntityDispatchEntry<Props extends object, S = unknown> = DispatchKeyRegistryEntry<
	EntityRefStub,
	Props,
	S
>;
export type ServiceDispatchEntry<Props extends object, S = unknown> = DispatchKeyRegistryEntry<
	ServiceRefStub,
	Props,
	S
>;

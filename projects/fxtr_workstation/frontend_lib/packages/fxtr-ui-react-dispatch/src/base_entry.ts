/**
 * `createBaseEntry` — the kernel used by every concrete
 * `Dispatcher` implementation to build a single `DispatchEntry`
 * from pre-wired session atoms.
 *
 * The caller (a specific dispatcher) is responsible for:
 *
 *   - Resolving the per-entry `stateAtom` (writable, typed by the
 *     state-machine's `S`) and `sharedComponentStateAtom`
 *     (writable, session-backed) — typically by projecting into
 *     the dispatcher's `userState` with `subAtom` /
 *     `atomWithDefault`.
 *   - Supplying the initial component, the initial progress
 *     message, the effect-fn factory body (`DispatchEffectFactory`),
 *     and the error-fallback component.
 *
 * `createBaseEntry` then owns the in-memory machinery:
 *
 *   - The dispatch progress message atom (in-memory only).
 *   - The component sink atom — writable, wrapped to enforce the
 *     **shared-state reset invariant** (any write that changes
 *     the component value also writes `null` to
 *     `sharedComponentStateAtom` in the same flush, so a freshly-
 *     resolved component sees fresh defaults from
 *     `useDispatchSharedAtom`).
 *   - The `atomEffect`-mounted effect-fn body.
 *   - The derived public `componentAtom` that, when read,
 *     subscribes to both the effect (mounting it) and the
 *     component sink (yielding the current value).
 *
 * The returned `DispatchEntry` exposes only the read-only side
 * of these atoms — the writables stay scoped to the closure and
 * are visible only through the `DispatchEffectFactory` parameter
 * surface (which is the contract for state-machine authors).
 */

import type { EntityRefStub, FxtrSessionAtom, ServiceRefStub, StateValue } from "@fxtr-ui/core";
import { type WritableAtom, atom } from "jotai";
import { atomEffect } from "jotai-effect";

import type { DispatchEntry } from "./dispatcher";
import type {
	DispatchEffectFactory,
	DispatchedComponent,
	ErrorFallbackComponent,
	StubFallbacks,
} from "./registry_types";

/**
 * One per-`(stub, key)` slot in a dispatcher's `userState`. The
 * static and dynamic dispatchers both use this shape under their
 * top-level `entries[keyUuid][shortId]` location. Each concrete
 * dispatcher's userState type wraps a `Record<keyUuid,
 * Record<shortId, EntryUserStateSlot>>` plus optional cross-entry
 * fields.
 *
 * - `dispatcher_state` is the helper's state-machine state,
 *   typed as `StateValue` (the wire-encodable supertype). Each
 *   helper's concrete `S` is cast on the way down to the
 *   effect-fn through the typed `stateAtom` parameter.
 * - `component_shared_state` is the `useDispatchSharedAtom`
 *   slot. Reset to `null` whenever the resolved component value
 *   changes — gives freshly-swapped components their own
 *   defaults rather than the previous one's leftover state.
 */
export interface EntryUserStateSlot {
	readonly dispatcher_state: StateValue;
	readonly component_shared_state: StateValue | null;
}

export interface CreateBaseEntryOptions<
	Stub extends EntityRefStub | ServiceRefStub,
	Props extends object,
	S,
> {
	/**
	 * Stable React key + cache id for the entry. The static
	 * dispatcher passes `${keyUuid}::${shortId}`; other
	 * dispatchers can use whatever uniqueness scheme they like.
	 */
	readonly id: string;
	readonly stub: Stub;
	/**
	 * Writable session-backed atom holding the state-machine's
	 * `S`. The caller is responsible for projecting it out of the
	 * dispatcher's `userState` (with `atomWithDefault` substituting
	 * the helper's `initialState` so reads never see undefined).
	 */
	readonly stateAtom: FxtrSessionAtom<S>;
	/**
	 * Writable session-backed atom holding the per-entry shared
	 * cache the dispatched component reads/writes through
	 * `useDispatchSharedAtom`. Reset to `null` by the
	 * shared-state reset invariant on every component swap.
	 */
	readonly sharedComponentStateAtom: FxtrSessionAtom<StateValue | null>;
	readonly initialComponent: DispatchedComponent<Stub, Props>;
	readonly initialProgressMessage: string;
	readonly effectFactory: DispatchEffectFactory<Stub, Props, S>;
	readonly ErrorFallback: ErrorFallbackComponent<Stub, Props>;
	/**
	 * Per-key fallback bundle, already merged with any dispatcher
	 * overrides. Passed through verbatim to the effect-fn factory
	 * as `params.fallbacks`. The dispatcher is responsible for
	 * deriving `initialComponent` (= `fallbacks.Loading`) and
	 * `ErrorFallback` (= `fallbacks.Error`) from this bundle —
	 * they're listed separately so the helper doesn't need to
	 * understand the bundle's shape.
	 */
	readonly fallbacks: StubFallbacks<Stub, Props>;
}

export function createBaseEntry<
	Stub extends EntityRefStub | ServiceRefStub,
	Props extends object,
	S,
>(opts: CreateBaseEntryOptions<Stub, Props, S>): DispatchEntry<Stub, Props> {
	const {
		id,
		stub,
		stateAtom,
		sharedComponentStateAtom,
		initialComponent,
		initialProgressMessage,
		effectFactory,
		ErrorFallback,
		fallbacks,
	} = opts;

	// In-memory progress message — transient UI hint, not
	// wire-persisted. If lost on reattach the state machine
	// re-sets it on the next transition.
	const progressMessageAtom = atom(initialProgressMessage);

	// Box the component reference: jotai's `atom(value)` and
	// `atom(read)` share a call signature, disambiguated by
	// whether the argument is a function. A React component IS
	// a function, so `atom<DispatchedComponent>(Fallback)` would
	// be interpreted as a derived atom with `Fallback` as its
	// read function — jotai would invoke `Fallback(get)` on the
	// first read, calling React hooks outside of a render and
	// crashing. Wrapping in an object makes the value non-
	// function so the plain-value overload picks up.
	type ComponentBox = { readonly Component: DispatchedComponent<Stub, Props> };
	const componentBoxAtom = atom<ComponentBox>({ Component: initialComponent });

	// Internal writable sink the effect-fn factory writes
	// through. Writes that change the value also reset the
	// shared sub-atom to `null` in the same flush — the
	// "componentAtom-changes-resets-shared" invariant. The
	// effect-fn factory receives this writable under the
	// parameter name `componentAtom` per the published
	// `DispatchEffectFactory` contract.
	const componentSinkAtom: WritableAtom<
		DispatchedComponent<Stub, Props>,
		[DispatchedComponent<Stub, Props>],
		void
	> = atom(
		(get) => get(componentBoxAtom).Component,
		(get, set, newValue: DispatchedComponent<Stub, Props>) => {
			const prev = get(componentBoxAtom).Component;
			set(componentBoxAtom, { Component: newValue });
			if (newValue !== prev) {
				set(sharedComponentStateAtom, null);
			}
		},
	);

	// Bind the registry-supplied effect-fn factory to the
	// entry's atoms, then wrap the resulting body in
	// `atomEffect`. Subscribing to `effectAtom` mounts the
	// effect and drives the state machine.
	const effectImpl = effectFactory({
		stub,
		stateAtom,
		componentAtom: componentSinkAtom,
		progressMessageAtom,
		fallbacks,
	});
	const effectAtom = atomEffect(effectImpl);

	// Public read-only `componentAtom`: depends on both
	// `effectAtom` (subscribing -> effect mounted) and
	// `componentSinkAtom` (the latest published component
	// value). `<RenderDispatch>` reads this to both render the
	// current component AND keep the state machine running.
	const componentAtom = atom((get): DispatchedComponent<Stub, Props> => {
		get(effectAtom);
		return get(componentSinkAtom);
	});

	return {
		id,
		stub,
		componentAtom,
		sharedComponentStateAtom,
		dispatchInternalStateAtom: stateAtom,
		dispatchProgressMessageAtom: progressMessageAtom,
		ErrorFallback,
	};
}

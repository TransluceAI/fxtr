/**
 * Hooks for dispatched components (rendered through
 * `<RenderDispatch>`). All three read from the
 * `DispatchEntryContext` that `<RenderDispatch>` sets up, and
 * throw if called outside that subtree — silent-null would let
 * stale `useAtomValue(entry.foo)` reads happen against the wrong
 * (or non-existent) entry.
 *
 * - `useDispatchSharedAtom<T>(default)` — lifts the entry's
 *   `sharedComponentStateAtom` (typed `FxtrSessionAtom<StateValue
 *   | null>`) through `atomWithDefault` so reads never see null.
 *   Mirrors `useSessionRootStateAtom` in `@fxtr-ui/react`: the
 *   default is captured once at mount, and the returned atom is
 *   identity-stable across re-renders. The shared atom is reset
 *   to `null` by the dispatcher whenever the entry's resolved
 *   component changes value, so a *new* rendered component
 *   naturally sees fresh defaults.
 *
 * - `useDispatchProgressState<S>()` — typed view of the entry's
 *   `dispatchInternalStateAtom` (the state-machine state). The
 *   `S` is unchecked at runtime — consumers should pass the
 *   state union exported alongside the registry-entry helper
 *   they used (e.g. `StaticEntityDispatchState`).
 *
 * - `useDispatchProgressMessage()` — generic progress string
 *   written by the state-machine effect. Lets a fallback
 *   component show sensible loading text without knowing the
 *   state machine's tagged-union shape.
 */

import type { FxtrSessionAtom } from "@fxtr-ui/core";
import { atomWithDefault } from "@fxtr-ui/state-util";
import { useAtomValue } from "jotai";
import { useMemo, useRef } from "react";
import { useDispatchEntryContext } from "./render_dispatch";

/**
 * Lift the active dispatch entry's `sharedComponentStateAtom`
 * to a typed non-null `FxtrSessionAtom<T>` by substituting
 * `initialDefault` when the underlying reads as null.
 *
 * `initialDefault` is captured once at mount (via `useRef`),
 * mirroring `useSessionRootStateAtom`. Accepts either a value
 * or a lazy `() => T` factory. The returned atom is identity-
 * stable across re-renders, so downstream `useSubAtom` /
 * `useAtomValue` consumers don't churn.
 *
 * TypeScript can't verify that `T` fits the wire `StateValue`
 * shape — same caveat as `useSessionRootStateAtom`. The
 * connector's wire codec is the runtime gate on encodability.
 */
export function useDispatchSharedAtom<T>(initialDefault: T | (() => T)): FxtrSessionAtom<T> {
	const entry = useDispatchEntryContext();

	// Capture once. Subsequent renders ignore changes to keep
	// the returned atom's identity stable.
	const defaultRef = useRef<{ readonly value: T } | null>(null);
	if (defaultRef.current === null) {
		const v = typeof initialDefault === "function" ? (initialDefault as () => T)() : initialDefault;
		defaultRef.current = { value: v };
	}
	const captured = defaultRef.current.value;

	return useMemo(
		() => atomWithDefault(entry.sharedComponentStateAtom as FxtrSessionAtom<T | null>, captured),
		// `captured` reference is stable for this hook's lifetime
		// (lives in a ref); `entry` changes only when the parent
		// `<RenderDispatch>` swaps in a new (stub, key) — which
		// also remounts the consumer (the entry-id `key` on the
		// context provider).
		[entry, captured],
	);
}

/**
 * Read the active dispatch entry's state-machine state.
 *
 * Each helper that builds a registry entry exports its
 * concrete `S` type alongside (e.g. `StaticEntityDispatchState`
 * from `static_entity_registry`). Fallback / ErrorBoundary
 * components colocated with the helper pass that type as the
 * `<S>` parameter:
 *
 *   const state = useDispatchProgressState<StaticEntityDispatchState>();
 *   if (state.phase === "loading-type-metadata") { … }
 *
 * The cast is unchecked at runtime; mismatches surface as
 * normal property-access errors on the consumer side.
 */
export function useDispatchProgressState<S>(): S {
	const entry = useDispatchEntryContext();
	return useAtomValue(entry.dispatchInternalStateAtom) as S;
}

/**
 * Read the active dispatch entry's progress-message string.
 * Written by the state-machine effect (`"loading entity…"`,
 * `"resolving renderer…"`, etc.); cleared on terminal phases.
 *
 * Intended for use in *generic* fallback components that don't
 * want to type-check against a specific state-machine's union
 * — e.g. a per-dispatch-key fallback that uses the same
 * "loading dots + status text" UI for any registry helper
 * paired with that key.
 */
export function useDispatchProgressMessage(): string {
	const entry = useDispatchEntryContext();
	return useAtomValue(entry.dispatchProgressMessageAtom);
}

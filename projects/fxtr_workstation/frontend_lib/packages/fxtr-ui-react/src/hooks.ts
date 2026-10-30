/**
 * Hooks for reading session state, projecting sub-atoms, and reading
 * stub results.
 *
 * Conventions:
 *
 *   - All hooks expect a `<FxtrSessionProvider>` somewhere above them
 *     in the tree (so `useAtomValue` / `useSetAtom` resolve against
 *     the session's store).
 *   - Read + write are jotai-native: `useAtomValue(atom)` /
 *     `useSetAtom(atom)`. The framework's contribution at the top
 *     level is `useSessionRootStateAtom(default)`, which returns the
 *     session's `userState` atom lifted to a non-null typed shape.
 *     From there, normal jotai composes — `useSubAtom` to project,
 *     plain `useAtomValue` to read.
 *   - `useSubAtom` is the React-layer companion to
 *     `@fxtr-ui/state-util`'s `subAtom`: memoizes the projection so
 *     the returned atom's identity is stable across renders.
 *   - `useResult` is one-line sugar over `useAtomValue(stub.result)`
 *     — included because the resulting call sites read more clearly.
 */

import type { FxtrSessionAtom, OpResultStub, ResultStatus } from "@fxtr-ui/core";
import { atomWithDefault, subAtom } from "@fxtr-ui/state-util";
import { useAtomValue } from "jotai";
import { useMemo, useRef } from "react";
import { useSession } from "./session_context";

/**
 * Lift the current session's `userState` atom (typed `FxtrSessionAtom<
 * UserState | null>`) to a typed `FxtrSessionAtom<T>` by substituting
 * `initialDefault` when the underlying value is null. Consumers
 * usually call this once at a panel's root component and pass the
 * derived atom / sub-atoms down.
 *
 * `initialDefault` accepts either a value or a lazy factory `() => T`
 * (mirroring `useState(initialValue)`). It is **captured once at
 * mount** via `useRef`; subsequent renders ignore changes. Reasons:
 * we need the returned atom's identity to be stable across re-renders
 * (downstream `useSubAtom`s depend on this) and we don't want a fresh
 * object literal to ripple through the whole tree. If you need a
 * truly dynamic default, compute it outside and write it into the
 * underlying atom directly.
 *
 * Read behavior: returns the parent's value when non-null; otherwise
 * the captured default. The default is *not* written back to the
 * parent on read — it's a read-time fallback. The wire sees a state
 * mutation only when the consumer actually writes.
 *
 * Write behavior: forwards through to the parent. A function-form
 * write `(prev, ctx) => next` sees `prev = parent ?? default`, so
 * the first write off a null underlying naturally seeds with defaults
 * plus whatever the updater layered on top.
 *
 * TypeScript can't verify that `T` fits the wire `StateValue`
 * shape (named-interface-vs-index-signature limitation), so the
 * underlying cast is unchecked. The connector's wire codec is the
 * actual runtime gate on encodability.
 */
export function useSessionRootStateAtom<T>(initialDefault: T | (() => T)): FxtrSessionAtom<T> {
	const session = useSession();
	// `useRef<T | typeof UNINITIALIZED>(UNINITIALIZED)` would be the
	// most precise pattern; using `T | undefined` with a manual check
	// is shorter and the inline initial-render branch is unambiguous.
	const defaultRef = useRef<{ readonly value: T } | null>(null);
	if (defaultRef.current === null) {
		const v = typeof initialDefault === "function" ? (initialDefault as () => T)() : initialDefault;
		defaultRef.current = { value: v };
	}
	const captured = defaultRef.current.value;
	return useMemo(
		() => atomWithDefault(session.userState as FxtrSessionAtom<T | null>, captured),
		// `captured` reference is stable for the lifetime of the
		// component (it lives in a ref); only `session` actually
		// changes when the upstream provider swaps.
		[session, captured],
	);
}

/**
 * Memoized `subAtom(parent, key)`. The returned atom is identity-
 * stable across renders for as long as `parent` and `key` don't
 * change.
 */
export function useSubAtom<T, K extends keyof T>(
	parent: FxtrSessionAtom<T>,
	key: K,
): FxtrSessionAtom<T[K]> {
	return useMemo(() => subAtom(parent, key), [parent, key]);
}

/**
 * Read a stub's `result` atom. Equivalent to
 * `useAtomValue(stub.result)`. Returns the `pending | filled |
 * errored` discriminated union — consumers branch on `.status`.
 */
export function useResult<T>(stub: OpResultStub<T>): ResultStatus<T> {
	return useAtomValue(stub.result);
}

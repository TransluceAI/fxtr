/**
 * Canonical "drive a load slot forward + observe its result" helper.
 *
 * The standard pattern: each renderer's session state has a load
 * slot — `FxtrSessionAtom<OpResultStub<T> | null>` — at some
 * sub-atom path. The renderer wants to (a) allocate the load if
 * the slot is null, (b) observe the result as a discriminated
 * union (loading / filled / errored), (c) render accordingly.
 *
 * `makeLoadResultAtom` returns ONE atom that does both halves:
 *
 *   - Internally it constructs an `atomEffect` driver that
 *     allocates the load on first read.
 *   - The atom's read fn `get`s the driver (mounting the effect
 *     when subscribed) and then projects the slot's result into
 *     a `LoadResult<T>`.
 *
 * Use it from inside a renderer's `useMemo`. Subscribing to the
 * returned atom via `useAtomValue` mounts the effect.
 *
 * Replaces the older `useEntityLoad` React hook that monopolized
 * `useDispatchSharedAtom` and used `useEffect` to drive slot
 * allocation. The atom form lets the caller pick where the slot
 * lives (carved out of an owning panel's `ownStorage` via
 * `subAtom`, projected from `useDispatchSharedAtom`, etc.), so
 * additional shared state composes alongside the load.
 */

import type { EntityRefStub, FxtrSessionAtom, OpResultStub } from "@fxtr-ui/core";
import { type Atom, atom } from "jotai";
import { atomEffect } from "jotai-effect";

export type LoadResult<T> =
	| { readonly kind: "loading" }
	| { readonly kind: "filled"; readonly value: T }
	| { readonly kind: "errored"; readonly message: string };

export function makeLoadResultAtom<T>(
	slotAtom: FxtrSessionAtom<OpResultStub<T> | null>,
	stub: EntityRefStub,
): Atom<LoadResult<T>> {
	const driver = atomEffect((get, set) => {
		if (get(slotAtom) === null) {
			set(slotAtom, (_prev, ctx) => ctx.allocateLoad<T>(stub as EntityRefStub<T>));
		}
	});
	return atom((get): LoadResult<T> => {
		// `get(driver)` mounts the effect when this atom is subscribed.
		// Don't remove — without it the load is never allocated.
		get(driver);
		const slot = get(slotAtom);
		if (slot === null) return { kind: "loading" };
		const r = get(slot.result);
		if (r.status === "filled") return { kind: "filled", value: r.value };
		if (r.status === "errored") return { kind: "errored", message: r.message };
		return { kind: "loading" };
	});
}

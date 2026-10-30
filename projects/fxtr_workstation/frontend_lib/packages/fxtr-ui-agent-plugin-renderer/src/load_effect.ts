/**
 * "Drive a load slot forward + observe its result" helper.
 *
 * Each per-event-type StreamEntry renderer needs to:
 *
 * 1. Allocate a load on the event ref (so the wire-decoded payload
 *    becomes available).
 * 2. Anchor the resulting :class:`OpResultStub` in dispatcher
 *    session state (so the session reducer doesn't GC the slot).
 * 3. Observe the slot's result as a discriminated union.
 *
 * :func:`makeLoadResultAtom` returns ONE jotai atom that does (1)
 * + (3) together. The slot atom — typically obtained via
 * :func:`useDispatchSharedAtom` in the calling renderer — holds the
 * :class:`OpResultStub` (or ``null`` before allocate); the returned
 * read atom mounts an :func:`atomEffect` driver that allocates the
 * load on first subscribe, then projects the slot's result.
 *
 * Same shape as the helper of the same name in
 * :mod:`@fxtr-ui/reports/load_effect`; inlined here so the renderer
 * package doesn't need to depend on reports just for one utility.
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

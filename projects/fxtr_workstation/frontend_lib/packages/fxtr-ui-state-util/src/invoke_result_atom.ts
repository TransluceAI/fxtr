/**
 * Canonical "drive an invoke slot forward + observe its result" helper —
 * the dual of `makeLoadResultAtom` for `allocateInvoke` instead of
 * `allocateLoad`.
 *
 * The standard pattern: a renderer's session state has an invoke slot —
 * `FxtrSessionAtom<OpResultStub<T> | null>` — at some sub-atom path. The
 * renderer wants to (a) issue the invoke once if the slot is null, (b)
 * observe its result as a `LoadResult<T>` (loading / filled / errored),
 * (c) render accordingly.
 *
 * `makeInvokeResultAtom` returns ONE atom that does both halves (the same
 * `atomEffect`-driver shape as `makeLoadResultAtom`): on first subscribe
 * it allocates the invoke when the slot is null, and its read fn projects
 * the slot's result into a `LoadResult<T>`. Subscribing to the returned
 * atom via `useAtomValue` mounts the effect.
 *
 * Use it from inside a renderer's `useMemo`, with the slot atom carved out
 * of an owning panel's `ownStorage` via `subAtom` (or projected from
 * `useDispatchSharedAtom`). Because the result event's refs stay pinned
 * while the slot is held in user state, this is also how a UI keeps a
 * service returned from an invoke (e.g. a channel subchannel reader)
 * alive: persist the slot, and the returned service stays resolvable.
 *
 * Unlike `allocateLoad`, invokes are **not** deduplicated — each distinct
 * slot drives its own invoke. Persist one slot per logical invoke.
 */

import type {
	FxtrSessionAtom,
	InvokeArgValue,
	Method,
	OpResultStub,
	ResultValue,
	ServiceRefStub,
} from "@fxtr-ui/core";
import { type Atom, atom } from "jotai";
import { atomEffect } from "jotai-effect";
import type { LoadResult } from "./load_result_atom";

/**
 * A throwaway one-method protocol shape so `allocateInvoke`'s typed
 * overload resolves the return type to `T` (any method name; loose args;
 * no kwargs). The caller already knows the method returns `T`; this just
 * threads that through the protocol-aware signature without demanding a
 * full declared protocol type.
 */
type InvokeReturning<T extends ResultValue> = {
	readonly [method: string]: Method<readonly InvokeArgValue[], Record<string, never>, T>;
};

export function makeInvokeResultAtom<T extends ResultValue>(
	slotAtom: FxtrSessionAtom<OpResultStub<T> | null>,
	svc: ServiceRefStub,
	method: string,
	args: readonly InvokeArgValue[],
): Atom<LoadResult<T>> {
	const driver = atomEffect((get, set) => {
		if (get(slotAtom) === null) {
			set(slotAtom, (_prev, ctx) =>
				ctx.allocateInvoke(svc as ServiceRefStub<InvokeReturning<T>>, method, args),
			);
		}
	});
	return atom((get): LoadResult<T> => {
		// `get(driver)` mounts the effect when this atom is subscribed.
		// Don't remove — without it the invoke is never issued.
		get(driver);
		const slot = get(slotAtom);
		if (slot === null) return { kind: "loading" };
		const r = get(slot.result);
		if (r.status === "filled") return { kind: "filled", value: r.value };
		if (r.status === "errored") return { kind: "errored", message: r.message };
		return { kind: "loading" };
	});
}

/**
 * Path-aware projection helper for `FxtrSessionAtom`s.
 *
 * Given a parent atom backing a value of type `T` and a key `K extends
 * keyof T`, produce a child atom over `T[K]` whose reads project into
 * the parent and whose writes propagate back as a structural update
 * with `K` replaced. The ctx threaded into function-form updaters
 * passes through unchanged, so consumers can `ctx.allocateLoad(...)`
 * inside a sub-atom updater and the resulting slot ends up referenced
 * inside the parent's user-state diff via the same session.
 *
 * The returned atom is a fresh `WritableAtom` instance per call;
 * memoize at the React-hook layer (see `useSubAtom` in
 * `@fxtr-ui/react`) to avoid spurious re-renders.
 *
 * Works for both object keys and numeric array indices (the latter
 * via runtime `Array.isArray` branching to preserve array shape on
 * write).
 *
 * **Undefined-handling semantics.** Writing `undefined` is treated
 * specially so it has clean wire-codec semantics downstream:
 *   - On an *object* parent, writing `undefined` removes the key
 *     entirely (`delete prev[key]`-style). This matches how the
 *     workstation's `escapeWire` / `diffStateToPatch` / `collectStubsInState`
 *     all treat an explicit-undefined field as absent. Removing the
 *     key keeps the in-memory shape consistent with what the wire
 *     sees, so a subsequent re-read returns `undefined` (or the
 *     parent's `atomWithDefault` substitution) rather than a phantom
 *     "field with value undefined."
 *   - On an *array* parent, writing `undefined` throws synchronously.
 *     Sparse-slot semantics break the wire codec — `escapeWire`'s
 *     array branch has no `undefined` arm and would throw on the
 *     following flush, which is harder to debug than the synchronous
 *     failure here. Consumers who want a sparse slot must lift the
 *     element type explicitly (e.g. `null` element, or wrap in
 *     `{value?: T}`).
 */

import type {
	FxtrSessionAtom,
	FxtrSessionUpdateCtx,
	FxtrUpdate,
	UserStateWriteOptions,
} from "@fxtr-ui/core";
import { atom } from "jotai";

export function subAtom<T, K extends keyof T>(
	parent: FxtrSessionAtom<T>,
	key: K,
): FxtrSessionAtom<T[K]> {
	return atom(
		(get) => get(parent)[key],
		(_get, set, update: FxtrUpdate<T[K]>, options?: UserStateWriteOptions) => {
			set(
				parent,
				(prevParent: T, ctx: FxtrSessionUpdateCtx): T => {
					const prevChild = prevParent[key];
					const newChild =
						typeof update === "function"
							? (update as (prev: T[K], ctx: FxtrSessionUpdateCtx) => T[K])(prevChild, ctx)
							: update;
					// Preserve array shape: `{ ...arr, [0]: x }` would
					// return a plain object with numeric-keyed properties,
					// which breaks both runtime expectations and the
					// session's structural diff. Branch on Array.isArray
					// and copy-then-assign for arrays. Objects spread as
					// usual.
					if (Array.isArray(prevParent)) {
						if (newChild === undefined) {
							// Sparse-undefined array slots crash
							// `escapeWire` downstream. Fail synchronously
							// here — the stack trace points at the
							// offending write rather than a later flush.
							throw new Error(
								`subAtom: writing \`undefined\` to array index ${String(key)} is not supported (undefined array elements break the wire codec). Use \`null\` for an explicit hole, or wrap the element type to express "absent."`,
							);
						}
						const arr = (prevParent as readonly unknown[]).slice();
						arr[key as unknown as number] = newChild;
						return arr as unknown as T;
					}
					if (newChild === undefined) {
						// On objects, write-undefined is "delete the
						// field." Matches the wire codec's treatment of
						// explicit-undefined fields as absent and keeps
						// the in-memory shape consistent with what the
						// wire sees.
						const { [key as unknown as string]: _omitted, ...rest } = prevParent as Record<
							string,
							unknown
						>;
						return rest as unknown as T;
					}
					return { ...prevParent, [key]: newChild };
				},
				options,
			);
		},
	);
}

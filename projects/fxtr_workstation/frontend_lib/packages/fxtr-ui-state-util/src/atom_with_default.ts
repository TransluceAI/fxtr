/**
 * Lift a `FxtrSessionAtom` over a possibly-nullish value to a
 * `FxtrSessionAtom<T>` by substituting a default when the
 * parent reads as `null` or `undefined`. Writes pass through
 * unchanged.
 *
 * The motivating use case: `session.userState` is typed
 * `FxtrSessionAtom<UserState | null>` (null until the first
 * snapshot lands). Consumers with a narrower shape want to do
 * things like `useSubAtom(userStateAtom, "invokes")`, but
 * `subAtom`'s `K extends keyof T` constraint can't drill into a
 * `T | null` value — `keyof (T | null) = never`. Lifting through
 * `atomWithDefault` once at the root component gives every sub-
 * atom downstream a non-null parent to drill into.
 *
 * Behavior:
 * - Read: `get(parent) ?? defaultValue`. If the parent's value is
 *   non-null, the default is ignored. If the parent reads null
 *   or undefined, the default is returned — never written to the
 *   parent atom. In particular: no "phantom mutation" happens on
 *   mount; the wire only sees a state mutation when the consumer
 *   actually writes through this atom (or directly to the parent).
 * - Write: `set(parent, update, options)`. The consumer-supplied
 *   `FxtrUpdate<T>` is forwarded verbatim. Function-form updaters
 *   see `prev = get(parent) ?? defaultValue` (i.e. the lifted
 *   read), so writing `setUserState(prev => ({ ...prev, x: 1 }))`
 *   on a null parent results in a write of `{ ...defaultValue, x:
 *   1 }`. This is what seeds the underlying atom on first write.
 *
 * Stale / incompatible persisted state — `validityCheck`:
 * Session state can outlive the code that wrote it (e.g. a panel's
 * `ownStorage` slot persisted by an older renderer, then re-read
 * after the renderer is replaced via `set_preset`). The persisted
 * value is non-null, so plain `??` substitution keeps it — and the
 * consumer then reads fields its current shape expects but the
 * stale object lacks. Pass an optional `validityCheck(value)` to
 * guard against this: a non-null parent value for which it returns
 * `false` is treated exactly like null — the default is
 * substituted (on both read and the function-updater `prev`). The
 * usual pattern is a top-level version tag on the stored shape
 * (`kind: "my-renderer/v1"`) whose check rejects a missing / wrong
 * tag, so a shape change becomes a clean reset instead of a crash.
 * Omitting `validityCheck` preserves the original null-only
 * behavior.
 *
 * Identity-stability: the returned atom is a fresh instance per
 * call. Memoize at the React layer (see `useSessionRootStateAtom` in
 * `@fxtr-ui/react`) or with `useMemo` if calling directly from
 * component code.
 */

import type {
	FxtrSessionAtom,
	FxtrSessionUpdateCtx,
	FxtrUpdate,
	UserStateWriteOptions,
} from "@fxtr-ui/core";
import { atom } from "jotai";

// `T` is the *output* shape. The parent is accepted as any of
// the four nullishness flavors — `FxtrSessionAtom<T>`,
// `FxtrSessionAtom<T | null>`, `FxtrSessionAtom<T | undefined>`,
// or `FxtrSessionAtom<T | null | undefined>` — via a union of
// parameter types. Each specific parent type matches exactly one
// arm; union-arm assignability sidesteps the `WritableAtom<X,
// ...>` invariance that blocks hardcoding any single widened
// shape.
//
// `NoInfer` on the parent param is load-bearing: without it, TS
// infers `T` from the parent's value type (picking the first
// matching arm), which for a `FxtrSessionAtom<U | null>` parent
// gives `T = U | null` — exactly the opposite of what we want.
// With `NoInfer`, `T` is inferred *only* from `defaultValue`
// (the actual output type), and the parent is then checked
// against the four arms with `T` already pinned.
//
// The implementation casts to the widest arm internally and
// reads / writes uniformly via `??` substitution.
export function atomWithDefault<T>(
	parent: NoInfer<
		| FxtrSessionAtom<T>
		| FxtrSessionAtom<T | null>
		| FxtrSessionAtom<T | undefined>
		| FxtrSessionAtom<T | null | undefined>
	>,
	defaultValue: T,
	validityCheck?: (value: T) => boolean,
): FxtrSessionAtom<T> {
	// All four parent arms behave identically at runtime —
	// `get`/`set` just read/write a value through the underlying
	// jotai atom and don't depend on TS's view of nullishness.
	// Casting to the widest arm lets the body operate uniformly.
	// The `unknown` bounce is required because the union of arms
	// isn't directly assignable to any single arm (invariance).
	const wide = parent as unknown as FxtrSessionAtom<T | null | undefined>;
	// A persisted value is usable only if it's non-null *and*
	// (when a check is supplied) passes it. Anything else falls
	// back to `defaultValue` — same code path null already took.
	const usable = (v: T | null | undefined): v is T =>
		v != null && (validityCheck === undefined || validityCheck(v));
	return atom(
		(get): T => {
			const v = get(wide);
			return usable(v) ? v : defaultValue;
		},
		(_get, set, update: FxtrUpdate<T>, options?: UserStateWriteOptions) => {
			if (typeof update === "function") {
				const fn = update as (prev: T, ctx: FxtrSessionUpdateCtx) => T;
				set(
					wide,
					(prev: T | null | undefined, ctx: FxtrSessionUpdateCtx) =>
						fn(usable(prev) ? prev : defaultValue, ctx),
					options,
				);
			} else {
				set(wide, update, options);
			}
		},
	);
}

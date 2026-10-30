/**
 * Per-entry "wrap on write" helper used by `createDynamicDispatcher`
 * to give dispatched components a staleness affordance during a hot
 * reload.
 *
 * The dynamic dispatcher mints a private `staleAtom: PrimitiveAtom<boolean>`
 * per entry; on a reload trigger the atom flips to `true`, and the
 * state machine clears it back to `false` once a new terminal phase
 * commits. `makeEntryWrapper(staleAtom)` returns a function that
 * wraps any `DispatchedComponent` in a small HOC subscribing to the
 * atom. While the atom reads `true` the wrap div applies opacity
 * `0.5`; while `false` it uses `display: contents` so the wrap has
 * no layout impact.
 *
 * The wrap is memoised via a `WeakMap` so `wrap(C) === wrap(C)` —
 * the base entry's "componentAtom value changed ⇒ reset
 * sharedComponentStateAtom" invariant fires exactly when the
 * underlying component changes, and React unmount/remount happens
 * at the same boundary it would without wrapping.
 *
 * Lives in its own `.tsx` file so the wrap component can be written
 * as plain JSX; `dynamic_dispatcher.ts` is `.ts`-only and imports
 * the helper from here.
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import { type Atom, useAtomValue } from "jotai";
import type { DispatchedComponent } from "./registry_types";

/**
 * Hidden symbol used to stamp the inner component identity on each
 * wrap. Tests use :func:`getStaleAwareInner` to recover the inner
 * for identity assertions ("did the dispatcher publish the right
 * component for this phase?") without depending on cosmetics like
 * `displayName`. Hidden via Symbol so production code can't reach
 * for it accidentally.
 */
const STALE_AWARE_INNER = Symbol("StaleAware.inner");

/**
 * Recover the inner component from a `StaleAware(*)` wrap, or
 * return `undefined` if the value isn't a wrapped component.
 *
 * Intended for tests that need to assert identity of the
 * dispatched component while letting production code keep the
 * wrap as an opaque `DispatchedComponent`. Reading the inner is
 * a deliberate test escape hatch.
 */
export function getStaleAwareInner<
	Stub extends EntityRefStub | ServiceRefStub,
	Props extends object,
>(wrapped: DispatchedComponent<Stub, Props>): DispatchedComponent<Stub, Props> | undefined {
	return (wrapped as unknown as { [STALE_AWARE_INNER]?: DispatchedComponent<Stub, Props> })[
		STALE_AWARE_INNER
	];
}

/**
 * Style applied to the wrap `<div>` when the entry is not stale —
 * `display: contents` removes the box so the wrap has no layout
 * impact while a fresh component is showing.
 */
const FRESH_STYLE = { display: "contents" } as const;

/**
 * Style applied to the wrap `<div>` when the entry is mid-reload —
 * `opacity` requires a real box, so we switch to `display: block`.
 * The brief layout flip is the visible signal that the dispatcher
 * is re-resolving; the inner component subtree's React identity
 * stays the same so it doesn't unmount.
 */
const STALE_STYLE = { display: "block", opacity: 0.5 } as const;

/**
 * The wrap function returned by :func:`makeEntryWrapper`. Generic
 * over the inner component's `Stub` and `Props` so the wrapped
 * version preserves its callsite-typed shape.
 */
export type EntryWrapper = <Stub extends EntityRefStub | ServiceRefStub, Props extends object>(
	Component: DispatchedComponent<Stub, Props>,
) => DispatchedComponent<Stub, Props>;

/**
 * Build a wrap helper that produces an identity-stable
 * `StaleAware(InnerComponent)` HOC for each inner component it
 * sees. Memoised via `WeakMap`. See the module docstring for the
 * invariants this preserves.
 */
export function makeEntryWrapper(staleAtom: Atom<boolean>): EntryWrapper {
	// biome-ignore lint/suspicious/noExplicitAny: per-(Stub,Props) heterogeneous cache keyed by raw component identity
	const cache = new WeakMap<DispatchedComponent<any, any>, DispatchedComponent<any, any>>();
	return <Stub extends EntityRefStub | ServiceRefStub, Props extends object>(
		Component: DispatchedComponent<Stub, Props>,
	): DispatchedComponent<Stub, Props> => {
		const existing = cache.get(Component);
		if (existing !== undefined) {
			return existing as DispatchedComponent<Stub, Props>;
		}
		const Wrapped: DispatchedComponent<Stub, Props> = (props) => {
			const stale = useAtomValue(staleAtom);
			return (
				<div style={stale ? STALE_STYLE : FRESH_STYLE}>
					<Component {...props} />
				</div>
			);
		};
		Wrapped.displayName = `StaleAware(${Component.displayName ?? Component.name ?? "Component"})`;
		// Stamp the inner identity so tests can recover it via
		// `getStaleAwareInner` without depending on `displayName`.
		(Wrapped as unknown as { [STALE_AWARE_INNER]: DispatchedComponent<Stub, Props> })[
			STALE_AWARE_INNER
		] = Component;
		cache.set(Component, Wrapped);
		return Wrapped;
	};
}

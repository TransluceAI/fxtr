/**
 * `<RenderDispatch>` — the consumer entrypoint to the dispatch
 * system. Given a `stub` (entity or service ref) and a
 * `DispatchKey`, it:
 *
 *   1. Looks up the active `Dispatcher` via
 *      `useDispatcher()` and asks it for the matching entry via
 *      `dispatcher.getEntry(dispatchKey, stub)`. All the heavy
 *      lifting (stub-kind detection, registry lookup, state-
 *      machine wiring, cache install) lives inside the dispatcher;
 *      `<RenderDispatch>` itself just retrieves the entry,
 *      subscribes, and renders.
 *   2. Subscribes to the entry's `componentAtom` (which both
 *      reads the current rendered component AND drives the
 *      state-machine effect).
 *   3. Renders the resolved component wrapped in:
 *        - `DispatchEntryContext.Provider` — so the dispatched
 *          component's hooks (`useDispatchSharedAtom`,
 *          `useDispatchProgressState`,
 *          `useDispatchProgressMessage`) can find their entry.
 *        - `SessionContext.Provider value={null}` — hides the
 *          parent's `useSession()` so the dispatched component
 *          can't accidentally allocate slots against the
 *          surrounding consumer's session.
 *        - `DispatchErrorBoundary` — catches render-time
 *          throws and renders the entry's `ErrorFallback`
 *          component.
 *
 * The prop type narrows the allowed `stub` kind from the
 * `DispatchKey`'s `Allowed` parameter: an entity-only key
 * statically rejects a service stub at the call site, and
 * vice versa. The dispatcher then picks the right runtime
 * resolution path by `instanceof`.
 *
 * The whole dispatched subtree is keyed on `entry.id` so a
 * change of stub or key remounts cleanly (fresh error-boundary
 * state, fresh `useDispatchSharedAtom` defaults).
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import { SessionContext } from "@fxtr-ui/react";
import { useAtomValue } from "jotai";
import { type ReactNode, createContext, useContext, useMemo } from "react";
import type { AnyDispatchEntry } from "./dispatcher";
import { useDispatcher } from "./dispatcher_provider";
import { DispatchErrorBoundary } from "./error_boundary";
import type { AnyDispatchKey, DispatchKey, ExtractDispatchProps } from "./keys";

// ---------------------------------------------------------------------------
// DispatchEntryContext — read by the dispatched-component-side hooks
// ---------------------------------------------------------------------------

export const DispatchEntryContext = createContext<AnyDispatchEntry | null>(null);

export function useDispatchEntryContext(): AnyDispatchEntry {
	const entry = useContext(DispatchEntryContext);
	if (entry === null) {
		throw new Error(
			"useDispatchEntryContext: no active <RenderDispatch> in the tree. The dispatch hooks (useDispatchSharedAtom / useDispatchProgressState / useDispatchProgressMessage) must be called inside a component rendered through <RenderDispatch>.",
		);
	}
	return entry;
}

// ---------------------------------------------------------------------------
// Prop-type narrowing: pick the allowed `stub` kind from the key's `Allowed`
// ---------------------------------------------------------------------------

/**
 * Map a dispatch key's `Allowed` parameter to the runtime
 * `stub` type RenderDispatch will accept. Tuple-wrap each
 * comparison to prevent conditional distribution. Uses
 * `DispatchKey<any, infer A>` for the same reason
 * `AnyDispatchKey` does — `Props`'s contravariance via
 * `fallbacks` makes the natural `object` upper bound reject
 * keys with non-trivial `Props`.
 */
// biome-ignore lint/suspicious/noExplicitAny: erased Props position, same rationale as AnyDispatchKey
type StubFor<K> = K extends DispatchKey<any, infer A>
	? ["entity"] extends [A]
		? ["service"] extends [A]
			? EntityRefStub | ServiceRefStub
			: EntityRefStub
		: ["service"] extends [A]
			? ServiceRefStub
			: never
	: never;

export type RenderDispatchProps<K extends AnyDispatchKey> = {
	readonly as: K;
	readonly stub: StubFor<K>;
} & ExtractDispatchProps<K>;

// ---------------------------------------------------------------------------
// RenderDispatch
// ---------------------------------------------------------------------------

export function RenderDispatch<K extends AnyDispatchKey>(props: RenderDispatchProps<K>): ReactNode {
	const dispatcher = useDispatcher();
	// Destructure outside the generic-typed prop view; the cast
	// to a plain Record matches React's standard prop-spread idiom.
	const {
		stub,
		as: dispatchKey,
		...extraProps
	} = props as RenderDispatchProps<K> & { stub: EntityRefStub | ServiceRefStub };
	const extraPropsErased = extraProps as Record<string, unknown>;

	// `dispatcher.getEntry` is idempotent for a given
	// `(dispatchKey, stub)` pair — repeat calls return the cached
	// entry. `useMemo` here avoids the dict-lookup cost on each
	// render. The function has no observable side effects before
	// the entry's atoms are subscribed to.
	const entry = useMemo(
		() => dispatcher.getEntry(dispatchKey, stub),
		[dispatcher, dispatchKey, stub],
	);

	// Subscribing to `componentAtom` drives both the effect
	// (state-machine progress) and the current rendered component.
	const Component = useAtomValue(entry.componentAtom) as React.ComponentType<
		Record<string, unknown>
	>;

	// `key={entry.id}` on the entry-context provider remounts the
	// entire dispatched subtree on (stub, key) change. Side benefit:
	// the error boundary's state resets, and any dispatched-component-
	// internal hooks re-capture defaults via the standard mount
	// lifecycle.
	return (
		<DispatchEntryContext.Provider key={entry.id} value={entry}>
			<SessionContext.Provider value={null}>
				<DispatchErrorBoundary
					stub={stub}
					extraProps={extraPropsErased}
					ErrorFallback={entry.ErrorFallback}
				>
					<Component stub={stub} {...extraPropsErased} />
				</DispatchErrorBoundary>
			</SessionContext.Provider>
		</DispatchEntryContext.Provider>
	);
}

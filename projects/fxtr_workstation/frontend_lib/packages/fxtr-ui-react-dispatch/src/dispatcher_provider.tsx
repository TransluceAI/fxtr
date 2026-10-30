/**
 * `<DispatcherProvider>` — the top-of-subtree component that
 * makes a `Dispatcher` available to the rest of the dispatch
 * React surface. A thin React-context wrapper: the dispatcher
 * itself owns its session, its userState projection, its
 * per-entry cache, and its state-machine implementations; the
 * provider just routes it to `<RenderDispatch>` (and indirectly
 * the dispatched-component hooks).
 *
 * The consumer constructs the dispatcher externally (typically
 * at app startup):
 *
 *   const dispatcher = createStaticDispatcher({session, registry});
 *   ...
 *   <DispatcherProvider dispatcher={dispatcher}>
 *     ...
 *   </DispatcherProvider>
 *
 * Single-store assumption: the dispatcher's session and the
 * surrounding React tree's `<JotaiProvider>` must share a jotai
 * store. Sessions default to their connector's store, so the
 * easy path is "use the connector's store everywhere" — that's
 * what the workstation demos do. Mismatched stores produce
 * silently-broken reads from `useDispatchSharedAtom` etc.;
 * treat as user error.
 */

import { type ReactNode, createContext, useContext } from "react";
import type { Dispatcher } from "./dispatcher";

export const DispatcherContext = createContext<Dispatcher | null>(null);

export function useDispatcher(): Dispatcher {
	const dispatcher = useContext(DispatcherContext);
	if (dispatcher === null) {
		throw new Error(
			"useDispatcher: no <DispatcherProvider> in the tree. <RenderDispatch> and the dispatch hooks must be rendered inside a <DispatcherProvider>.",
		);
	}
	return dispatcher;
}

export interface DispatcherProviderProps {
	readonly dispatcher: Dispatcher;
	readonly children: ReactNode;
}

export function DispatcherProvider({ dispatcher, children }: DispatcherProviderProps): ReactNode {
	return <DispatcherContext.Provider value={dispatcher}>{children}</DispatcherContext.Provider>;
}

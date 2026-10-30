/**
 * React-side delivery of the current `Session` and its jotai store.
 *
 * `<FxtrSessionProvider session={s}>` wraps two things together:
 *
 *   1. A jotai `<Provider store={s.store}>` — every `useAtomValue` /
 *      `useSetAtom` / `useAtom` call inside the provider tree reads
 *      from / writes to the session's own store. Critically, the
 *      session's atoms (`session.userState`, `session.lifecycle`,
 *      etc.) are constructed with internal atoms (`localAtom`,
 *      `committedAtom`, …) whose values only exist in `session.store`,
 *      so any other store wouldn't have meaningful values for them.
 *      Component-internal atoms (`useMemo(() => atom(...), [])`) are
 *      also scoped to this store — two panels backed by different
 *      `Session` instances have fully isolated component-internal
 *      state.
 *
 *   2. A React Context carrying the `Session` itself — accessible
 *      via `useSession()`. The context approach delivers the session
 *      synchronously at render time; we don't need
 *      `useHydrateAtoms` (the session pre-populated its own store in
 *      its constructor; nothing to hydrate from React's side).
 *
 * `useSession()` throws if called outside a `<FxtrSessionProvider>`.
 * A silent-null return would let stale `useAtomValue(session.userState)`
 * reads happen against the wrong store — confusing footgun. Throwing
 * lands on the obvious offender.
 */

import type { Session } from "@fxtr-ui/core";
import { Provider as JotaiProvider } from "jotai";
import { type ReactNode, createContext, useContext } from "react";

/**
 * The React Context that `<FxtrSessionProvider>` writes to and
 * `useSession()` reads from. Exported so other packages (e.g.
 * `@fxtr-ui/react-dispatch`) can set it to `null` to "hide" the
 * parent's session at a subtree boundary — calls to
 * `useSession()` inside that subtree then throw the standard
 * "no provider" error, preventing inadvertent reads of the
 * outer session.
 */
export const SessionContext = createContext<Session | null>(null);

export interface FxtrSessionProviderProps {
	readonly session: Session;
	readonly children: ReactNode;
}

export function FxtrSessionProvider({ session, children }: FxtrSessionProviderProps) {
	return (
		<JotaiProvider store={session.store}>
			<SessionContext.Provider value={session}>{children}</SessionContext.Provider>
		</JotaiProvider>
	);
}

/**
 * Read the current `Session` from context. Throws if called outside a
 * `<FxtrSessionProvider>`. The returned reference is stable across
 * renders for as long as the provider's `session` prop doesn't change.
 */
export function useSession(): Session {
	const s = useContext(SessionContext);
	if (s === null) {
		throw new Error(
			"useSession() called outside a <FxtrSessionProvider>. Wrap the consumer in <FxtrSessionProvider session={...}>.",
		);
	}
	return s;
}

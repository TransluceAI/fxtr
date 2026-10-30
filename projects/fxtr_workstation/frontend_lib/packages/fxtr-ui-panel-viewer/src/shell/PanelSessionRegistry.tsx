/**
 * `PanelSessionRegistry` — context the shell uses to track each
 * panel's live session by dockview panel id.
 *
 * Each `PanelWrapper` calls `register(panelId, session)` when its
 * lifecycle hook transitions to `live`, and runs the returned
 * unregister fn on cleanup (lifecycle changes, unmount). The
 * shell's `PanelViewerApi` implementation looks up panel sessions
 * here when it needs to read or write a panel's chain state in
 * response to a navigation event.
 *
 * **Reactive surface.** The registry exposes its full map as a
 * jotai atom (`sessions`) so consumers that need to observe
 * register / unregister events — most notably the open-panels
 * sidebar, which mirrors each panel's active stub through its
 * session userState — can subscribe via `useAtomValue`. The
 * imperative `register` / `get` methods stay for callers that
 * don't need reactivity (e.g. the shell's nav-event handlers,
 * which read the current map snapshot at fire time).
 *
 * Why a separate context (rather than rolling this into the
 * `PanelViewerApi`): the registration surface is shell-internal
 * orchestration, not part of the public navigation contract that
 * descendants invoke. Keeping them separate lets a test mount a
 * `PanelWrapper` with a stub registry without standing up a full
 * shell, and keeps the public `PanelViewerApi` focused on the
 * three navigation methods.
 *
 * Provider: `PanelViewer`. Consumers: `PanelWrapper`,
 * `OpenPanelsSidebar`.
 */

import type { Session } from "@fxtr-ui/core";
import type { Atom } from "jotai";
import { type ReactNode, createContext, useContext } from "react";

export interface PanelSessionRegistry {
	/**
	 * Register a panel's live session. Returns an unregister
	 * function. Calling `register` twice for the same `panelId`
	 * replaces the previous registration; the previous
	 * unregister fn becomes a no-op.
	 */
	register(panelId: string, session: Session): () => void;

	/**
	 * Look up the live session for a panel. Returns `undefined`
	 * if the panel has no session registered (e.g. it's still in
	 * pending state, an info panel without a session, or it was
	 * never a ref panel). Synchronous read of the current map
	 * snapshot — does not subscribe.
	 */
	get(panelId: string): Session | undefined;

	/**
	 * Reactive view of the full panel-id → session map. The
	 * underlying map is re-built on each register / unregister so
	 * `useAtomValue(sessions)` re-renders subscribers when a
	 * panel goes live or unmounts.
	 */
	readonly sessions: Atom<ReadonlyMap<string, Session>>;
}

const PanelSessionRegistryContext = createContext<PanelSessionRegistry | null>(null);

export interface PanelSessionRegistryProviderProps {
	readonly value: PanelSessionRegistry;
	readonly children: ReactNode;
}

export function PanelSessionRegistryProvider({
	value,
	children,
}: PanelSessionRegistryProviderProps) {
	return (
		<PanelSessionRegistryContext.Provider value={value}>
			{children}
		</PanelSessionRegistryContext.Provider>
	);
}

/**
 * Read the panel-session registry. Returns `null` outside a
 * shell — `PanelWrapper` falls back to a no-op registration in
 * that case so it's still testable in isolation.
 */
export function usePanelSessionRegistry(): PanelSessionRegistry | null {
	return useContext(PanelSessionRegistryContext);
}

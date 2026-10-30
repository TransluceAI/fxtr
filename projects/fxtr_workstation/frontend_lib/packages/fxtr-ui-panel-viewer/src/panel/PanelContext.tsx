/**
 * PanelContext — tells descendants which dockview panel they live
 * in AND, for ref panels, what that panel's current chain /
 * active position / historyRoot are.
 *
 * Provided only by content-renderer wrappers for panel kinds
 * where link-follow navigation makes sense. Currently that's only
 * `PanelWrapper` for `kind: "ref"` panels. The roots
 * panel intentionally does NOT provide this context — its
 * descendants see `usePanelContext() === null`, and the
 * interaction hooks use that signal to route through
 * `restoreIntoNewPanel(computeSnapshotFromTrail(...))` instead of
 * `followLink`.
 *
 * Drag hooks (for breadcrumb entries and content refs) read the
 * chain state at drag-start to build the drag payload. Clicks
 * only use `panelId` directly but the full state travels through
 * the same context for consistency.
 *
 * The chain / active / historyRoot live in the panel's session
 * userState — `PanelWrapper` reads them out of session via
 * `useSessionRootStateAtom` and threads the resulting snapshot
 * down through this context. Descendants reading the context get
 * the latest committed view because the context value re-renders
 * whenever the session-backed atom updates.
 */

import { type ReactNode, createContext, useContext } from "react";
import type { HistoryRoot } from "../breadcrumb/types.js";
import type { PanelViewEntry } from "../types.js";

/**
 * Which kind of dockview panel the descendant lives in.
 * Currently:
 *   - `"ref"` — a `PanelWrapper` showing one chain of refs.
 *   - `"roots"` — the registry browser (does NOT provide
 *     `PanelContext`; included here for future kinds).
 */
export type PanelKind = "ref" | "roots";

export interface PanelContextValue {
	readonly panelId: string;
	readonly kind: PanelKind;
	readonly chain: ReadonlyArray<PanelViewEntry>;
	readonly activeIndex: number;
	readonly historyRoot: HistoryRoot;
}

const PanelContext = createContext<PanelContextValue | null>(null);

export interface PanelContextProviderProps {
	readonly value: PanelContextValue;
	readonly children: ReactNode;
}

export function PanelContextProvider({ value, children }: PanelContextProviderProps) {
	return <PanelContext.Provider value={value}>{children}</PanelContext.Provider>;
}

export function usePanelContext(): PanelContextValue | null {
	return useContext(PanelContext);
}

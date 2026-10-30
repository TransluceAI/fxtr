/**
 * PanelContext — tells descendants which dockview panel they live in
 * AND what that panel's current chain / active / historyRoot are.
 *
 * Provided only by content-renderer wrappers for panel kinds where
 * link-follow navigation makes sense. Currently that's only
 * `EntityPanelWrapper` (`kind: "entity"`). The NamespacePanel
 * intentionally does NOT provide this context — its descendants see
 * `usePanelContext() === null`, and the interaction hooks use that
 * signal to route through `restoreIntoNewPanel` (with
 * `computeSnapshotFromTrail`) instead of `followLink`.
 *
 * Drag hooks (for breadcrumb entries and content refs) read the
 * chain/active/historyRoot fields at drag-start to build the drag
 * payload. Clicks only use `panelId` directly but the full state
 * travels through the same context for consistency.
 */

import { type ReactNode, createContext, useContext } from "react";
import type { HistoryRoot } from "../breadcrumb/types.js";

export type PanelKind = "entity";

export interface PanelContextValue {
	panelId: string;
	kind: PanelKind;
	chain: string[];
	activeIndex: number;
	historyRoot: HistoryRoot;
}

const PanelContext = createContext<PanelContextValue | null>(null);

export interface PanelContextProviderProps {
	value: PanelContextValue;
	children: ReactNode;
}

export function PanelContextProvider({ value, children }: PanelContextProviderProps) {
	return <PanelContext.Provider value={value}>{children}</PanelContext.Provider>;
}

export function usePanelContext(): PanelContextValue | null {
	return useContext(PanelContext);
}

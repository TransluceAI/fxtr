/**
 * PanelViewerContext — exposes the shell's navigation API to any
 * descendant that wants to open / replace / activate panels in
 * response to user input.
 *
 * Provider: `PanelViewer`.
 * Consumers: `useRefClick`, `useRefContextMenu`,
 * `useDraggableRef`, `useDraggableChainEntry`,
 * `useDraggableHistoryEntry`, breadcrumb entries, history-root
 * entries, and `DockviewLayout`'s drop handler (via direct prop,
 * since imperative event handlers can't pull context cleanly).
 *
 * `usePanelViewer()` throws when used outside a shell-mounted
 * tree.
 */

import { type ReactNode, createContext, useContext } from "react";
import type { PanelViewerApi } from "./PanelViewerApi.js";

const PanelViewerContext = createContext<PanelViewerApi | null>(null);

export interface PanelViewerProviderProps {
	readonly value: PanelViewerApi;
	readonly children: ReactNode;
}

export function PanelViewerProvider({ value, children }: PanelViewerProviderProps) {
	return <PanelViewerContext.Provider value={value}>{children}</PanelViewerContext.Provider>;
}

export function usePanelViewer(): PanelViewerApi {
	const ctx = useContext(PanelViewerContext);
	if (ctx === null) {
		throw new Error("usePanelViewer must be used inside a <PanelViewer>");
	}
	return ctx;
}

/**
 * WorkstationContext — exposes the shell's navigation API to any
 * descendant that wants to open / replace / activate panels in response
 * to user input.
 *
 * Providers: `WorkstationShell`.
 * Consumers: `useEntityClick`, `useEntityContextMenu`,
 * `useDraggableEntity`, `useDraggableChainEntry`,
 * `useDraggableHistoryEntry`, `BreadcrumbEntry`, `HistoryRootEntry`,
 * and `DockviewLayout`'s drop handler (via direct prop, to avoid
 * pulling context through imperative event handlers).
 */

import { type ReactNode, createContext, useContext } from "react";
import type { WorkstationApi } from "./navigate.js";

const WorkstationContext = createContext<WorkstationApi | null>(null);

export interface WorkstationProviderProps {
	value: WorkstationApi;
	children: ReactNode;
}

export function WorkstationProvider({ value, children }: WorkstationProviderProps) {
	return <WorkstationContext.Provider value={value}>{children}</WorkstationContext.Provider>;
}

export function useWorkstation(): WorkstationApi {
	const ctx = useContext(WorkstationContext);
	if (!ctx) {
		throw new Error("useWorkstation must be used inside a <WorkstationShell>");
	}
	return ctx;
}

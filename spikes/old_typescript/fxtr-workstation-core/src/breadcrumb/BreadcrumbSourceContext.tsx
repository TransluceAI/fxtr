/**
 * BreadcrumbSourceContext — React context carrying the partial trail.
 *
 * `EntityPanelWrapper`, `useEntityHoverCard`, and `NamespacePanel` rows
 * each wrap their children in a provider so nested clicks/drags can
 * read back the trail assembled so far.
 */

import { type ReactNode, createContext, useContext } from "react";
import type { BreadcrumbSource } from "./types.js";

const BreadcrumbSourceContext = createContext<BreadcrumbSource | null>(null);

export interface BreadcrumbSourceProviderProps {
	value: BreadcrumbSource | null;
	children: ReactNode;
}

export function BreadcrumbSourceProvider({ value, children }: BreadcrumbSourceProviderProps) {
	return (
		<BreadcrumbSourceContext.Provider value={value}>{children}</BreadcrumbSourceContext.Provider>
	);
}

export function useBreadcrumbSource(): BreadcrumbSource | null {
	return useContext(BreadcrumbSourceContext);
}

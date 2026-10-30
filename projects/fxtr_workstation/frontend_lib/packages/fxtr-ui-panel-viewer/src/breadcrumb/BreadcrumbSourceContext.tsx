/**
 * BreadcrumbSourceContext — React context carrying the partial
 * trail.
 *
 * Each surface that introduces a navigation source wraps its
 * children in a provider so nested clicks / drags can read back
 * the trail assembled so far:
 *
 *   - `PanelWrapper` (ref panels) provides one with the active
 *     ref as `currentKey`.
 *   - `useRefHoverCard` provides one inside the popup body,
 *     extending the surrounding chain with a hover-ref segment.
 *   - The roots panel provides one per row (with a null
 *     `currentKey`) so trail building starts fresh from the
 *     click.
 */

import { type ReactNode, createContext, useContext } from "react";
import type { BreadcrumbSource } from "./types.js";

const BreadcrumbSourceContext = createContext<BreadcrumbSource | null>(null);

export interface BreadcrumbSourceProviderProps {
	readonly value: BreadcrumbSource | null;
	readonly children: ReactNode;
}

export function BreadcrumbSourceProvider({ value, children }: BreadcrumbSourceProviderProps) {
	return (
		<BreadcrumbSourceContext.Provider value={value}>{children}</BreadcrumbSourceContext.Provider>
	);
}

export function useBreadcrumbSource(): BreadcrumbSource | null {
	return useContext(BreadcrumbSourceContext);
}

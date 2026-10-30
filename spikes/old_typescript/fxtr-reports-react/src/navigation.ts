/**
 * Report navigation context: breadcrumb stack, navigateTo, navigateBack.
 *
 * Navigation rules:
 * - navigateTo with a new entityId pushes a new breadcrumb entry
 * - navigateTo with the same entityId but different navState replaces the top entry
 * - navigateBack(index) pops back to that index, discarding everything after
 */

import type { FxtrID } from "@fxtr/core";
import { createContext, useContext } from "react";
import type { BreadcrumbEntry, JsonValue } from "./types.js";

// ---------------------------------------------------------------------------
// Public interface
// ---------------------------------------------------------------------------

export interface ReportNavigation {
	/** Navigate to an entity, optionally with navState. */
	navigateTo(entityId: FxtrID, navState?: JsonValue): void;
	/** Pop back to a specific breadcrumb index, discarding everything after. */
	navigateBack(index: number): void;
	/** The full breadcrumb stack. Last entry is current. */
	breadcrumbs: readonly BreadcrumbEntry[];
	/** The current entry, or null if showing the initial view (no navigation yet). */
	currentEntry: BreadcrumbEntry | null;
}

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

export const ReportNavigationContext = createContext<ReportNavigation | null>(null);

/**
 * Access the report navigation context.
 * Must be called inside a `<ReportShell>`.
 */
export function useReportNavigation(): ReportNavigation {
	const ctx = useContext(ReportNavigationContext);
	if (!ctx) {
		throw new Error("useReportNavigation must be called inside a <ReportShell>");
	}
	return ctx;
}

// ---------------------------------------------------------------------------
// State reducer
// ---------------------------------------------------------------------------

export interface NavigationState {
	breadcrumbs: BreadcrumbEntry[];
}

export type NavigationAction =
	| { type: "navigate"; entityId: FxtrID; navState?: JsonValue | undefined }
	| { type: "back"; index: number }
	| { type: "restore"; breadcrumbs: BreadcrumbEntry[] };

export function navigationReducer(
	state: NavigationState,
	action: NavigationAction,
): NavigationState {
	switch (action.type) {
		case "navigate": {
			const newEntry: BreadcrumbEntry = {
				entityId: action.entityId,
				navState: action.navState,
			};

			const current =
				state.breadcrumbs.length > 0 ? state.breadcrumbs[state.breadcrumbs.length - 1] : undefined;

			// Same entity, just updating navState — replace top entry
			if (current && current.entityId.key() === action.entityId.key()) {
				const updated = [...state.breadcrumbs];
				updated[updated.length - 1] = newEntry;
				return { breadcrumbs: updated };
			}

			// New entity — push
			return {
				breadcrumbs: [...state.breadcrumbs, newEntry],
			};
		}
		case "back": {
			if (action.index >= state.breadcrumbs.length) {
				return state;
			}
			if (action.index < 0) {
				return { breadcrumbs: [] };
			}
			return {
				breadcrumbs: state.breadcrumbs.slice(0, action.index + 1),
			};
		}
		case "restore": {
			return { breadcrumbs: action.breadcrumbs };
		}
	}
}

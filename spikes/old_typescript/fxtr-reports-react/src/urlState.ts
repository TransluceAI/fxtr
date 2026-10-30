/**
 * URL synchronization for report navigation state.
 *
 * URL scheme:
 *   /                → card list view
 *   /<idString>      → main view for entity
 *   /<idString>?navState=<json>&breadcrumbs=<json>  → with state
 *
 * Breadcrumbs are encoded as JSON: [{id: "<idString>", navState?: <json>}, ...]
 */

import { FxtrID } from "@fxtr/core";
import type { BreadcrumbEntry, JsonValue } from "./types.js";

// ---------------------------------------------------------------------------
// Serialized format for breadcrumbs in the URL
// ---------------------------------------------------------------------------

interface SerializedBreadcrumb {
	id: string;
	navState?: JsonValue;
}

// ---------------------------------------------------------------------------
// Parse URL → navigation state
// ---------------------------------------------------------------------------

export interface ParsedUrlState {
	/** The entity ID from the path, or null if at root (card list). */
	entityId: FxtrID | null;
	/** navState from query param. */
	navState: JsonValue | undefined;
	/** Breadcrumb entries from query param. */
	breadcrumbs: BreadcrumbEntry[];
}

export function parseUrlState(): ParsedUrlState {
	const path = window.location.pathname;
	const params = new URLSearchParams(window.location.search);

	// Parse entity ID from path
	// Strip leading slash, ignore empty segments
	const idString = path.replace(/^\/+/, "");
	const entityId = idString ? FxtrID.fromIdString(idString) : null;

	// Parse navState
	let navState: JsonValue | undefined;
	const navStateParam = params.get("navState");
	if (navStateParam !== null) {
		try {
			navState = JSON.parse(navStateParam) as JsonValue;
		} catch {
			// Ignore malformed navState
		}
	}

	// Parse breadcrumbs
	const breadcrumbs: BreadcrumbEntry[] = [];
	const bcParam = params.get("breadcrumbs");
	if (bcParam !== null) {
		try {
			const parsed = JSON.parse(bcParam) as SerializedBreadcrumb[];
			for (const entry of parsed) {
				const bcId = FxtrID.fromIdString(entry.id);
				const bc: BreadcrumbEntry = { entityId: bcId };
				if (entry.navState !== undefined) {
					bc.navState = entry.navState;
				}
				breadcrumbs.push(bc);
			}
		} catch {
			// Ignore malformed breadcrumbs
		}
	}

	return { entityId, navState, breadcrumbs };
}

// ---------------------------------------------------------------------------
// Navigation state → URL string
// ---------------------------------------------------------------------------

export function buildUrl(
	entityId: FxtrID | null,
	navState: JsonValue | undefined,
	breadcrumbs: readonly BreadcrumbEntry[],
): string {
	if (!entityId) return "/";

	const path = `/${entityId.idString()}`;
	const params = new URLSearchParams();

	if (navState !== undefined) {
		params.set("navState", JSON.stringify(navState));
	}

	if (breadcrumbs.length > 0) {
		const serialized: SerializedBreadcrumb[] = breadcrumbs.map((bc) => {
			const entry: SerializedBreadcrumb = { id: bc.entityId.idString() };
			if (bc.navState !== undefined) {
				entry.navState = bc.navState;
			}
			return entry;
		});
		params.set("breadcrumbs", JSON.stringify(serialized));
	}

	const search = params.toString();
	return search ? `${path}?${search}` : path;
}

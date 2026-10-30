/**
 * Breadcrumb + history types.
 *
 * Two concerns live here:
 *
 * 1. The `Breadcrumb[]` **trail** — how the user got from a source
 *    entity to a target across panel / hover / embed navigation. Built
 *    incrementally through React via `BreadcrumbSource`
 *    (see BreadcrumbSourceContext.tsx).
 *
 * 2. The **per-panel history state** — `EntityPanelParams`'s
 *    `{chain, activeIndex, historyRoot}` triple that each entity
 *    panel persists. `HistoryRoot` is recursive: each `branched`
 *    node carries the parent panel's chain-up-to-active at the
 *    moment of branching, plus that parent's own historyRoot.
 *    `{ kind: "namespace" }` is the base case.
 */

import type { FxtrID } from "@fxtr/core";

/** Where a breadcrumb segment was produced. */
export type BreadcrumbVia =
	| "panel-ref" // ref inside an entity panel
	| "hover-ref" // ref inside a hover preview card
	| "namespace-panel" // row click in the namespace panel
	| "context-menu"; // right-click → "Open in panel"

/**
 * A single trail segment. `fromEntityId` is omitted for entry-point taps
 * (e.g. clicking a pin in the namespace panel).
 */
export interface Breadcrumb {
	fromEntityId?: string;
	toEntityId: string;
	via: BreadcrumbVia;
}

/**
 * Context value describing the partial breadcrumb trail assembled so far
 * by the surrounding panel + any nested hover cards.
 *
 * - `pendingBreadcrumbs` is the trail built up from surrounding contexts.
 * - `currentKey` is the entity "we are looking at right now"; if
 *   non-null, the next navigation appends a segment `{ from: currentKey,
 *   to: target, via }`. If null (e.g. namespace rows), no `from` segment
 *   is appended — the trail starts fresh at the click.
 * - `via` is the type to stamp on the next appended segment.
 */
export interface BreadcrumbSource {
	pendingBreadcrumbs: Breadcrumb[];
	currentKey: FxtrID | null;
	via: BreadcrumbVia;
}

/**
 * Assemble the final trail for a navigation event that targets `target`.
 *
 * If `source.currentKey` is non-null, append a final segment
 * `{ from: currentKey, to: target, via }`.
 * If it is null, emit a length-1 trail `[{ to: target, via }]` (no
 * `from` segment).
 */
export function buildTrail(source: BreadcrumbSource | null, target: FxtrID): Breadcrumb[] {
	const targetIdString = target.idString();
	if (!source) {
		return [{ toEntityId: targetIdString, via: "panel-ref" }];
	}
	const finalSegment: Breadcrumb =
		source.currentKey === null
			? { toEntityId: targetIdString, via: source.via }
			: {
					fromEntityId: source.currentKey.idString(),
					toEntityId: targetIdString,
					via: source.via,
				};
	return [...source.pendingBreadcrumbs, finalSegment];
}

// ---------------------------------------------------------------------------
// Per-panel history state.
// ---------------------------------------------------------------------------

/**
 * Recursive structure describing how the panel was reached before its
 * own chain started. `{ namespace }` is the base case — a pin click
 * from the namespace panel (or any other non-panel origin) yields this.
 * `{ branched, prefix, parent }` captures a branch from an entity panel:
 * `prefix` is the parent panel's chain up to (and including) its active
 * entity at the moment of branching, and `parent` is that parent
 * panel's own historyRoot.
 *
 * The structure is preserved across history-root-menu navigation — we
 * peel off layers rather than flattening, so that restoring from a
 * deeper ancestor re-exposes the remaining layers as the new panel's
 * historyRoot. See `computeOpenNewFromHistoryEntry`.
 */
export type HistoryRoot =
	| { kind: "namespace" }
	| { kind: "branched"; prefix: string[]; parent: HistoryRoot };

/**
 * Entity-panel params as stored in dockview. All fields are JSON-safe
 * (plain strings/numbers/object literals) so `toJSON`/`fromJSON`
 * round-trip cleanly for layout persistence. `FxtrID`s and other
 * class instances never live here.
 *
 * Invariants:
 * - `chain.length >= 1`
 * - `0 <= activeIndex < chain.length`
 *
 * `EntityPanelWrapper` is the only consumer that hydrates the active
 * `FxtrID` via `FxtrID.fromIdString(chain[activeIndex])`.
 */
export interface EntityPanelParams {
	chain: string[];
	activeIndex: number;
	historyRoot: HistoryRoot;
}

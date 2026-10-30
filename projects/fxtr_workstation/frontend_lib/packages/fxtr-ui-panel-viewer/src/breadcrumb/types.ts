/**
 * Breadcrumb + history types — generalized over `PanelViewEntry`
 * (stub-bearing in-process form) so the workstation chain renders
 * either kind of ref interchangeably and naturally records each
 * entry's display mode (default dispatched view vs. hardcoded
 * metadata view).
 *
 * Two concerns live here:
 *
 * 1. The `Breadcrumb[]` **trail** — how the user got from a source
 *    ref to a target across panel / hover / row navigation. Built
 *    incrementally through React via `BreadcrumbSource` (see
 *    `BreadcrumbSourceContext.tsx`).
 *
 * 2. The **per-panel chain state** — `RefPanelChainState`'s
 *    `{chain, activeIndex, historyRoot}` triple that each ref
 *    panel persists *inside its session userState* (not in
 *    dockview params). `HistoryRoot` is recursive: each `branched`
 *    node carries the parent panel's chain-up-to-active at the
 *    moment of branching, plus that parent's own historyRoot.
 *    `{ kind: "roots" }` is the base case for a chain originating
 *    from the roots panel (or any other non-panel surface).
 *
 * Both `Breadcrumb` and `HistoryRoot.branched.prefix` reference
 * `PanelViewEntry` (stub-bearing). The session reducer's
 * `collectStubsInState` walks them and Track-pins every ref reachable
 * through the panel's navigation structure — so anything the user
 * can still click on through breadcrumb / history-root navigation
 * is necessarily kept alive server-side.
 *
 * For drag payloads — which cross a `JSON.stringify` boundary — see
 * the sister types `BreadcrumbDragData` / the `PanelViewDragData`
 * pair in `../types.ts`; conversion helpers below.
 */

import type { Connector } from "@fxtr-ui/core";
import {
	type PanelViewDragData,
	type PanelViewEntry,
	dragDataOfEntry,
	entryOfDragData,
} from "../types.js";

/** Where a breadcrumb segment was produced. */
export type BreadcrumbVia =
	| "panel-ref" // ref inside a ref panel
	| "hover-ref" // ref inside a hover preview card
	| "roots-panel" // row click in the roots panel
	| "context-menu"; // right-click → "Open in new panel"

/**
 * A single trail segment. `from` is omitted for entry-point taps
 * (e.g. clicking a roots-panel row).
 */
export interface Breadcrumb {
	readonly from?: PanelViewEntry;
	readonly to: PanelViewEntry;
	readonly via: BreadcrumbVia;
}

/**
 * JSON-safe sister of `Breadcrumb` for drag payloads. Round-trips
 * through `JSON.stringify` losslessly; lift to `Breadcrumb` on the
 * receiving side via `breadcrumbOfDragData(connector)`.
 */
export interface BreadcrumbDragData {
	readonly from?: PanelViewDragData;
	readonly to: PanelViewDragData;
	readonly via: BreadcrumbVia;
}

/** Lower a `Breadcrumb` to the JSON-safe drag shape. */
export function dragDataOfBreadcrumb(b: Breadcrumb): BreadcrumbDragData {
	return {
		...(b.from !== undefined ? { from: dragDataOfEntry(b.from) } : {}),
		to: dragDataOfEntry(b.to),
		via: b.via,
	};
}

/** Lift a `BreadcrumbDragData` back to a `Breadcrumb`. */
export function breadcrumbOfDragData(d: BreadcrumbDragData, connector: Connector): Breadcrumb {
	return {
		...(d.from !== undefined ? { from: entryOfDragData(d.from, connector) } : {}),
		to: entryOfDragData(d.to, connector),
		via: d.via,
	};
}

/**
 * Context value describing the partial breadcrumb trail assembled
 * so far by the surrounding panel + any nested hover cards.
 *
 * - `pendingBreadcrumbs` — the trail built up from surrounding
 *   contexts.
 * - `currentKey` — the ref "we are looking at right now"; if
 *   non-null, the next navigation appends a segment
 *   `{ from: currentKey, to: target, via }`. If null (e.g. roots
 *   rows), no `from` segment is appended — the trail starts fresh
 *   at the click.
 * - `via` — the type to stamp on the next appended segment.
 */
export interface BreadcrumbSource {
	readonly pendingBreadcrumbs: ReadonlyArray<Breadcrumb>;
	readonly currentKey: PanelViewEntry | null;
	readonly via: BreadcrumbVia;
}

/**
 * Assemble the final trail for a navigation event that targets
 * `target`.
 *
 * If `source.currentKey` is non-null, append a final segment
 * `{ from: currentKey, to: target, via }`.  If it is null, emit a
 * length-1 trail `[{ to: target, via }]` (no `from` segment).
 *
 * If `source` itself is null (no surrounding panel / hover), emit
 * a length-1 trail with `via = "panel-ref"` as a placeholder; the
 * trail still routes correctly, but no caller surface should be
 * relying on the `via` tag in that case.
 */
export function buildTrail(source: BreadcrumbSource | null, target: PanelViewEntry): Breadcrumb[] {
	if (source === null) {
		return [{ to: target, via: "panel-ref" }];
	}
	const finalSegment: Breadcrumb =
		source.currentKey === null
			? { to: target, via: source.via }
			: { from: source.currentKey, to: target, via: source.via };
	return [...source.pendingBreadcrumbs, finalSegment];
}

// ---------------------------------------------------------------------------
// Per-panel chain state.
// ---------------------------------------------------------------------------

/**
 * Recursive structure describing how the panel was reached before
 * its own chain started. `{ roots }` is the base case — a row
 * click from the roots panel (or any other non-panel origin)
 * yields this. `{ branched, prefix, parent }` captures a branch
 * from a ref panel: `prefix` is the parent panel's chain up to
 * (and including) its active ref at the moment of branching, and
 * `parent` is that parent panel's own historyRoot.
 *
 * `prefix` references stub-bearing `PanelViewEntry`s — so every
 * ancestor entry in every history-root layer is Track-pinned by
 * the panel session, and a click on a deep history-root-menu
 * entry weeks later still loads cleanly even though no slot
 * explicitly references it.
 *
 * The structure is preserved across history-root-menu navigation
 * — we peel off layers rather than flattening, so that restoring
 * from a deeper ancestor re-exposes the remaining layers as the
 * new panel's historyRoot. See `computeOpenNewFromHistoryEntry`
 * in `history.ts`.
 */
export type HistoryRoot =
	| { readonly kind: "roots" }
	| {
			readonly kind: "branched";
			readonly prefix: ReadonlyArray<PanelViewEntry>;
			readonly parent: HistoryRoot;
	  };

/**
 * The user-state shape a ref panel's session owns. Lives inside
 * the panel session's `userState`. `PanelWrapper` lifts this
 * via `useSessionRootStateAtom` and exposes the read view to
 * descendants through `PanelContext`.
 *
 * Invariants:
 *   - `chain.length >= 1` once the panel has been seeded with a
 *     target.
 *   - `0 <= activeIndex < chain.length`.
 *   - `storages.length === chain.length`. `storages[i]` is the
 *     renderer state for `chain[i]`.
 *
 * `storages[i]` is whatever the renderer for `chain[i]` chose to
 * put there — opaque from the shell's POV. The renderer narrows
 * on its end via `atomWithDefault` + `useSubAtom`. Each chain
 * entry has exactly one renderer for its lifetime (entries with
 * `display: "default"` go through the dispatched FullPanel;
 * entries with `display: "metadata"` go through the hardcoded
 * metadata view — and the `display` field is part of entry
 * identity, so the renderer is fixed at creation), so a single
 * storage slot per entry is sufficient.
 *
 * Liveness rides on the stubs inside `chain` (and on those
 * inside `historyRoot.branched.prefix`, recursively) —
 * `collectStubsInState` walks them and the session reducer emits
 * Track ops for every reachable ref. The `storages` array is a
 * pure cache: clearing any `storages[i]` is safe (the entry's
 * stub is still tracked); the renderer will re-allocate on next
 * mount if needed.
 */
export interface RefPanelChainState {
	readonly chain: ReadonlyArray<PanelViewEntry>;
	readonly activeIndex: number;
	readonly historyRoot: HistoryRoot;
	readonly storages: ReadonlyArray<unknown>;
}

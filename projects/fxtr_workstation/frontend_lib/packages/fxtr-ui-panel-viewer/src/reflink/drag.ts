/**
 * Drag-and-drop constants + payload helpers.
 *
 * Drag payloads serialize through `JSON.stringify`, which would
 * lose the class identity of any stub instance carried inside. So
 * the wire shape of every payload uses the JSON-safe `PanelViewDragData`
 * / `BreadcrumbDragData` types from `../types.ts` and
 * `../breadcrumb/types.ts`. Drag-source hooks lower stub-bearing
 * `PanelViewEntry`s to drag-data before calling `setDragData`; the
 * shell's drop handler (`DockviewLayout.tsx`) lifts back to
 * `PanelViewEntry` via `entryOfDragData(connector)` /
 * `breadcrumbOfDragData(connector)` before applying.
 *
 * Drag payloads are discriminated to mirror the `followLink` vs
 * `restoreIntoNewPanel` split in the workstation API:
 *
 *   - `LinkDragPayload` — produced by content-ref drags inside a
 *     panel (or inside a non-panel surface that has a trail).
 *     Drop → the shell calls `followLink` with `intent: open-new`
 *     + position (for panel sources) or
 *     `restoreIntoNewPanel(computeSnapshotFromTrail(...))` (for
 *     non-panel sources).
 *
 *   - `SnapshotDragPayload` — produced by breadcrumb-entry and
 *     history-root drags, which have a fully-resolved new chain
 *     state at drag-start. Drop →
 *     `restoreIntoNewPanel(state, position)`.
 *
 * The payload carries a `text/plain` fallback with the target
 * `shortId` so drop targets that only speak text receive
 * something sensible.
 */

import { DRAG_MIME_FXTR_REF } from "@fxtr-ui/workstation-dispatch";
import type { BreadcrumbDragData, HistoryRoot, RefPanelChainState } from "../breadcrumb/types.js";
import type { PanelViewDragData } from "../types.js";

export { DRAG_MIME_FXTR_REF };

/**
 * JSON-safe variant of `HistoryRoot`. Mirrors the recursive
 * structure but replaces stub-bearing `PanelViewEntry` in
 * `prefix` with the wire-safe `PanelViewDragData` form.
 */
export type HistoryRootDragData =
	| { readonly kind: "roots" }
	| {
			readonly kind: "branched";
			readonly prefix: ReadonlyArray<PanelViewDragData>;
			readonly parent: HistoryRootDragData;
	  };

/**
 * JSON-safe snapshot of a `RefPanelChainState`'s navigation
 * structure (chain + activeIndex + historyRoot). The drop handler
 * re-lifts to `RefPanelChainState` via per-stub interning through
 * the connector; storages are *not* round-tripped — they'd hold
 * source-session-scoped `OpResultStub`s that would substitute to
 * broken-sentinels in the destination session anyway.
 */
export interface SnapshotChainDragData {
	readonly chain: ReadonlyArray<PanelViewDragData>;
	readonly activeIndex: number;
	readonly historyRoot: HistoryRootDragData;
}

export interface LinkDragPayload {
	readonly kind: "link";
	/** Target entry (matches `trail[trail.length-1].to`). */
	readonly target: PanelViewDragData;
	readonly trail: ReadonlyArray<BreadcrumbDragData>;
	/** Present when the drag started from inside a ref panel. */
	readonly sourcePanelId?: string;
}

export interface SnapshotDragPayload {
	readonly kind: "snapshot";
	/** Wire-safe summary of the new chain state. */
	readonly newPanelState: SnapshotChainDragData;
	/** = `newPanelState.chain[newPanelState.activeIndex]`. */
	readonly target: PanelViewDragData;
	/** Present when the drag originated inside a panel (for logging). */
	readonly sourcePanelId?: string;
}

export type DragPayload = LinkDragPayload | SnapshotDragPayload;

export function setDragData(dataTransfer: DataTransfer, payload: DragPayload): void {
	dataTransfer.setData(DRAG_MIME_FXTR_REF, JSON.stringify(payload));
	dataTransfer.setData("text/plain", payload.target.shortId);
	dataTransfer.effectAllowed = "copy";
}

function isPanelViewDragData(v: unknown): v is PanelViewDragData {
	if (v === null || typeof v !== "object") return false;
	const r = v as { kind?: unknown; shortId?: unknown; display?: unknown };
	return (
		(r.kind === "entity" || r.kind === "service") &&
		typeof r.shortId === "string" &&
		(r.display === "default" || r.display === "metadata")
	);
}

function isHistoryRootDragData(v: unknown): v is HistoryRootDragData {
	if (v === null || typeof v !== "object") return false;
	const r = v as { kind?: unknown; prefix?: unknown; parent?: unknown };
	if (r.kind === "roots") return true;
	if (r.kind !== "branched") return false;
	if (!Array.isArray(r.prefix)) return false;
	if (!r.prefix.every(isPanelViewDragData)) return false;
	return isHistoryRootDragData(r.parent);
}

function isSnapshotChainDragData(v: unknown): v is SnapshotChainDragData {
	if (v === null || typeof v !== "object") return false;
	const r = v as { chain?: unknown; activeIndex?: unknown; historyRoot?: unknown };
	if (!Array.isArray(r.chain)) return false;
	if (!r.chain.every(isPanelViewDragData)) return false;
	if (typeof r.activeIndex !== "number") return false;
	return isHistoryRootDragData(r.historyRoot);
}

export function parseDragData(dataTransfer: DataTransfer): DragPayload | null {
	const raw = dataTransfer.getData(DRAG_MIME_FXTR_REF);
	if (!raw) return null;
	try {
		const parsed = JSON.parse(raw) as unknown;
		if (parsed === null || typeof parsed !== "object") return null;
		const p = parsed as Partial<DragPayload> & { kind?: unknown; target?: unknown };
		if (!isPanelViewDragData(p.target)) return null;
		if (p.kind === "link" && Array.isArray((p as Partial<LinkDragPayload>).trail)) {
			return p as LinkDragPayload;
		}
		if (
			p.kind === "snapshot" &&
			isSnapshotChainDragData((p as Partial<SnapshotDragPayload>).newPanelState)
		) {
			return p as SnapshotDragPayload;
		}
	} catch {
		// fall through
	}
	return null;
}

/**
 * Test a DataTransfer *before* getData is legal (i.e. during
 * dragover).
 */
export function hasDragData(dataTransfer: DataTransfer): boolean {
	return dataTransfer.types.includes(DRAG_MIME_FXTR_REF);
}

// ---------------------------------------------------------------------------
// HistoryRoot ↔ HistoryRootDragData conversion helpers
// ---------------------------------------------------------------------------

import type { Connector } from "@fxtr-ui/core";
import { dragDataOfEntry, entryOfDragData } from "../types.js";

/**
 * Lower a live `HistoryRoot` to its JSON-safe variant by walking
 * the recursive structure and converting each `prefix` entry.
 */
export function dragDataOfHistoryRoot(root: HistoryRoot): HistoryRootDragData {
	if (root.kind === "roots") return { kind: "roots" };
	return {
		kind: "branched",
		prefix: root.prefix.map(dragDataOfEntry),
		parent: dragDataOfHistoryRoot(root.parent),
	};
}

/**
 * Lift a `HistoryRootDragData` back to a live `HistoryRoot` by
 * re-interning each `prefix` entry's stub through the connector.
 */
export function historyRootOfDragData(
	data: HistoryRootDragData,
	connector: Connector,
): HistoryRoot {
	if (data.kind === "roots") return { kind: "roots" };
	return {
		kind: "branched",
		prefix: data.prefix.map((d) => entryOfDragData(d, connector)),
		parent: historyRootOfDragData(data.parent, connector),
	};
}

// ---------------------------------------------------------------------------
// RefPanelChainState ↔ SnapshotChainDragData conversion helpers
// ---------------------------------------------------------------------------

/**
 * Lower a `RefPanelChainState` to its JSON-safe snapshot form for
 * inclusion in a `SnapshotDragPayload`. Drops `storages` — they'd
 * hold source-session-scoped `OpResultStub`s that the destination
 * session's flush walk would substitute to broken-sentinels
 * anyway.
 */
export function snapshotChainDragDataOf(state: RefPanelChainState): SnapshotChainDragData {
	return {
		chain: state.chain.map(dragDataOfEntry),
		activeIndex: state.activeIndex,
		historyRoot: dragDataOfHistoryRoot(state.historyRoot),
	};
}

/**
 * Lift a `SnapshotChainDragData` back to a `RefPanelChainState` by
 * re-interning each chain stub through the connector. Storages
 * are initialized to `null`s (the wire codec doesn't accept
 * `undefined` array elements, and `atomWithDefault` treats both
 * as the absent sentinel); the destination's renderers will
 * allocate fresh.
 */
export function refPanelChainStateOfDragData(
	data: SnapshotChainDragData,
	connector: Connector,
): RefPanelChainState {
	const chain = data.chain.map((d) => entryOfDragData(d, connector));
	return {
		chain,
		activeIndex: data.activeIndex,
		historyRoot: historyRootOfDragData(data.historyRoot, connector),
		storages: chain.map(() => null),
	};
}

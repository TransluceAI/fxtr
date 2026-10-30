/**
 * Drag-and-drop constants + payload helpers.
 *
 * Drag payloads are discriminated to mirror the `followLink` vs
 * `restoreIntoNewPanel` split in the workstation API:
 *
 * - `LinkDragPayload` — produced by content-ref drags inside a panel
 *   (or inside a non-panel surface that has a trail). Drop → the shell
 *   calls `followLink` with `intent: open-new + position` (for panel
 *   sources) or `restoreIntoNewPanel(computeSnapshotFromTrail(...))`
 *   (for non-panel sources).
 * - `SnapshotDragPayload` — produced by breadcrumb-entry and history-
 *   root drags, which have a fully-resolved new panel state at
 *   drag-start. Drop → `restoreIntoNewPanel(state, position)`.
 *
 * The payload carries a `text/plain` fallback with the target id string
 * so drop targets that only speak text receive something sensible.
 */

import type { Breadcrumb, EntityPanelParams } from "../breadcrumb/types.js";

export const DRAG_MIME_FXTR_ENTITY = "application/x-fxtr-entity";

export interface LinkDragPayload {
	kind: "link";
	/** Target entity id string (matches `trail[trail.length-1].toEntityId`). */
	entityIdString: string;
	trail: Breadcrumb[];
	/** Present when the drag started from inside an entity panel. */
	sourcePanelId?: string;
}

export interface SnapshotDragPayload {
	kind: "snapshot";
	/** Fully-resolved state the drop target should open. */
	newPanelState: EntityPanelParams;
	/** = `newPanelState.chain[newPanelState.activeIndex]`. */
	entityIdString: string;
	/** Present when the drag originated inside a panel (for logging). */
	sourcePanelId?: string;
}

export type DragPayload = LinkDragPayload | SnapshotDragPayload;

export function setDragData(dataTransfer: DataTransfer, payload: DragPayload): void {
	dataTransfer.setData(DRAG_MIME_FXTR_ENTITY, JSON.stringify(payload));
	dataTransfer.setData("text/plain", payload.entityIdString);
	dataTransfer.effectAllowed = "copy";
}

export function parseDragData(dataTransfer: DataTransfer): DragPayload | null {
	const raw = dataTransfer.getData(DRAG_MIME_FXTR_ENTITY);
	if (!raw) return null;
	try {
		const parsed = JSON.parse(raw) as unknown;
		if (!parsed || typeof parsed !== "object") return null;
		const p = parsed as Partial<DragPayload> & { kind?: unknown };
		if (p.kind === "link" && typeof p.entityIdString === "string" && Array.isArray(p.trail)) {
			return p as LinkDragPayload;
		}
		if (
			p.kind === "snapshot" &&
			typeof p.entityIdString === "string" &&
			p.newPanelState &&
			typeof p.newPanelState === "object"
		) {
			return p as SnapshotDragPayload;
		}
	} catch {
		// fall through
	}
	return null;
}

/** Test a DataTransfer *before* getData is legal (i.e. during dragover). */
export function hasDragData(dataTransfer: DataTransfer): boolean {
	return dataTransfer.types.includes(DRAG_MIME_FXTR_ENTITY);
}

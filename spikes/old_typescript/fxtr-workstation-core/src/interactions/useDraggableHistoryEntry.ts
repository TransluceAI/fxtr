/**
 * useDraggableHistoryEntry — drag-source wiring for a single entry in
 * the history-root menu. Packs a `SnapshotDragPayload` with state
 * pre-computed via `computeOpenNewFromHistoryEntry`; drop →
 * `restoreIntoNewPanel`.
 */

import type { DragEvent as ReactDragEvent } from "react";
import { useCallback } from "react";
import { type FlatHistoryEntry, computeOpenNewFromHistoryEntry } from "../breadcrumb/history.js";
import { type DragPayload, setDragData } from "./drag.js";

export interface UseDraggableHistoryEntryArgs {
	entry: FlatHistoryEntry;
	sourcePanelId?: string | undefined;
}

export interface UseDraggableHistoryEntryResult {
	draggable: true;
	onDragStart: (e: ReactDragEvent) => void;
}

export function useDraggableHistoryEntry({
	entry,
	sourcePanelId,
}: UseDraggableHistoryEntryArgs): UseDraggableHistoryEntryResult {
	const onDragStart = useCallback(
		(e: ReactDragEvent) => {
			if (!e.dataTransfer) return;
			const newPanelState = computeOpenNewFromHistoryEntry(entry);
			const entityIdString = newPanelState.chain[newPanelState.activeIndex];
			if (entityIdString === undefined) return;
			const payload: DragPayload = {
				kind: "snapshot",
				entityIdString,
				newPanelState,
				...(sourcePanelId !== undefined ? { sourcePanelId } : {}),
			};
			setDragData(e.dataTransfer, payload);
		},
		[entry, sourcePanelId],
	);

	return { draggable: true, onDragStart };
}

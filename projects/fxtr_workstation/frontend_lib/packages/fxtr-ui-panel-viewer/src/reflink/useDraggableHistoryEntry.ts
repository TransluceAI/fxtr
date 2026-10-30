/**
 * useDraggableHistoryEntry — drag-source wiring for a single
 * entry in the history-root menu. Packs a `SnapshotDragPayload`
 * with state pre-computed via `computeOpenNewFromHistoryEntry`;
 * drop → `restoreIntoNewPanel`.
 *
 * The state is lowered to `SnapshotChainDragData` at the
 * `setDragData` boundary; the drop handler lifts back via the
 * connector.
 */

import type { DragEvent as ReactDragEvent } from "react";
import { useCallback } from "react";
import { type FlatHistoryEntry, computeOpenNewFromHistoryEntry } from "../breadcrumb/history.js";
import { dragDataOfEntry } from "../types.js";
import { type DragPayload, setDragData, snapshotChainDragDataOf } from "./drag.js";

export interface UseDraggableHistoryEntryArgs {
	readonly entry: FlatHistoryEntry;
	readonly sourcePanelId?: string | undefined;
}

export interface UseDraggableHistoryEntryResult {
	readonly draggable: true;
	readonly onDragStart: (e: ReactDragEvent) => void;
}

export function useDraggableHistoryEntry({
	entry,
	sourcePanelId,
}: UseDraggableHistoryEntryArgs): UseDraggableHistoryEntryResult {
	const onDragStart = useCallback(
		(e: ReactDragEvent) => {
			if (!e.dataTransfer) return;
			const newPanelState = computeOpenNewFromHistoryEntry(entry);
			const target = newPanelState.chain[newPanelState.activeIndex];
			if (target === undefined) return;
			const payload: DragPayload = {
				kind: "snapshot",
				target: dragDataOfEntry(target),
				newPanelState: snapshotChainDragDataOf(newPanelState),
				...(sourcePanelId !== undefined ? { sourcePanelId } : {}),
			};
			setDragData(e.dataTransfer, payload);
		},
		[entry, sourcePanelId],
	);

	return { draggable: true, onDragStart };
}

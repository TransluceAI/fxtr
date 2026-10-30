/**
 * useDraggableChainEntry — drag-source wiring for a breadcrumb
 * entry.
 *
 * Breadcrumb entries don't follow a link (the ref is already in
 * the panel's chain), so they pack a `SnapshotDragPayload` with
 * state pre-computed via `computeOpenNewFromChainEntry`. Drop →
 * `restoreIntoNewPanel`.
 *
 * The state is lowered to `SnapshotChainDragData` at the
 * `setDragData` boundary; the drop handler lifts back via the
 * connector.
 */

import type { DragEvent as ReactDragEvent } from "react";
import { useCallback } from "react";
import { computeOpenNewFromChainEntry } from "../breadcrumb/history.js";
import type { RefPanelChainState } from "../breadcrumb/types.js";
import { dragDataOfEntry } from "../types.js";
import { type DragPayload, setDragData, snapshotChainDragDataOf } from "./drag.js";

export interface UseDraggableChainEntryArgs {
	readonly sourcePanelId: string;
	readonly panelState: RefPanelChainState;
	readonly chainIndex: number;
}

export interface UseDraggableChainEntryResult {
	readonly draggable: true;
	readonly onDragStart: (e: ReactDragEvent) => void;
}

export function useDraggableChainEntry({
	sourcePanelId,
	panelState,
	chainIndex,
}: UseDraggableChainEntryArgs): UseDraggableChainEntryResult {
	const onDragStart = useCallback(
		(e: ReactDragEvent) => {
			if (!e.dataTransfer) return;
			const newPanelState = computeOpenNewFromChainEntry(panelState, chainIndex);
			const target = newPanelState.chain[newPanelState.activeIndex];
			if (target === undefined) return;
			const payload: DragPayload = {
				kind: "snapshot",
				target: dragDataOfEntry(target),
				newPanelState: snapshotChainDragDataOf(newPanelState),
				sourcePanelId,
			};
			setDragData(e.dataTransfer, payload);
		},
		[sourcePanelId, panelState, chainIndex],
	);

	return { draggable: true, onDragStart };
}

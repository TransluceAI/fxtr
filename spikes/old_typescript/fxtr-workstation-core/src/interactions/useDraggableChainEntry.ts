/**
 * useDraggableChainEntry — drag-source wiring for a breadcrumb entry.
 *
 * Breadcrumb entries don't follow a link (the entity is already in the
 * panel's chain), so they pack a `SnapshotDragPayload` with state
 * pre-computed via `computeOpenNewFromChainEntry`. Drop →
 * `restoreIntoNewPanel`.
 */

import type { DragEvent as ReactDragEvent } from "react";
import { useCallback } from "react";
import { computeOpenNewFromChainEntry } from "../breadcrumb/history.js";
import type { EntityPanelParams } from "../breadcrumb/types.js";
import { type DragPayload, setDragData } from "./drag.js";

export interface UseDraggableChainEntryArgs {
	sourcePanelId: string;
	panelState: EntityPanelParams;
	chainIndex: number;
}

export interface UseDraggableChainEntryResult {
	draggable: true;
	onDragStart: (e: ReactDragEvent) => void;
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
			const entityIdString = newPanelState.chain[newPanelState.activeIndex];
			if (entityIdString === undefined) return;
			const payload: DragPayload = {
				kind: "snapshot",
				entityIdString,
				newPanelState,
				sourcePanelId,
			};
			setDragData(e.dataTransfer, payload);
		},
		[sourcePanelId, panelState, chainIndex],
	);

	return { draggable: true, onDragStart };
}

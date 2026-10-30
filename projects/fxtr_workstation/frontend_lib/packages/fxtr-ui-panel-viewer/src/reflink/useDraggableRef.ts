/**
 * useDraggableRef — drag-source wiring for a content ref.
 *
 * Branches on `usePanelContext`:
 *
 *   - Inside a panel: packs a `LinkDragPayload` (target ref,
 *     trail, sourcePanelId). `followLink` at drop time reads the
 *     source panel's current state and derives the new chain.
 *   - Outside a panel: packs a `SnapshotDragPayload` built via
 *     `computeSnapshotFromTrail`. Drop → `restoreIntoNewPanel`.
 *
 * Stub-bearing payloads are lowered to the JSON-safe drag-data
 * types at the `setDragData` boundary (see `drag.ts`); the drop
 * handler lifts back via `entryOfDragData(connector)`.
 *
 * Used by both `RefLink` (whose whole root is draggable) and
 * `DraggableRefHandle` (a grip inside a FullPanel).
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import type { DragEvent as ReactDragEvent } from "react";
import { useCallback } from "react";
import { useBreadcrumbSource } from "../breadcrumb/BreadcrumbSourceContext.js";
import { computeSnapshotFromTrail } from "../breadcrumb/history.js";
import { buildTrail, dragDataOfBreadcrumb } from "../breadcrumb/types.js";
import { usePanelContext } from "../panel/PanelContext.js";
import { dragDataOfEntry, panelViewEntryOf } from "../types.js";
import { type DragPayload, setDragData, snapshotChainDragDataOf } from "./drag.js";

export interface UseDraggableRefResult {
	readonly draggable: true;
	readonly onDragStart: (e: ReactDragEvent) => void;
}

export function useDraggableRef(stub: EntityRefStub | ServiceRefStub): UseDraggableRefResult {
	const source = useBreadcrumbSource();
	const panel = usePanelContext();

	const onDragStart = useCallback(
		(e: ReactDragEvent) => {
			if (!e.dataTransfer) return;
			const target = panelViewEntryOf(stub);
			const trail = buildTrail(source, target);
			const payload: DragPayload = panel
				? {
						kind: "link",
						target: dragDataOfEntry(target),
						trail: trail.map(dragDataOfBreadcrumb),
						sourcePanelId: panel.panelId,
					}
				: {
						kind: "snapshot",
						target: dragDataOfEntry(target),
						newPanelState: snapshotChainDragDataOf(computeSnapshotFromTrail(trail)),
					};
			setDragData(e.dataTransfer, payload);
		},
		[stub, source, panel],
	);

	return { draggable: true, onDragStart };
}

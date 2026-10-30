/**
 * useDraggableEntity — drag-source wiring for a content entity ref.
 *
 * Branches on `usePanelContext`:
 *
 * - Inside a panel: packs a `LinkDragPayload` (target id, trail,
 *   sourcePanelId). `followLink` at drop time reads the source panel's
 *   current state and derives the new chain.
 * - Outside a panel: packs a `SnapshotDragPayload` built via
 *   `computeSnapshotFromTrail`. Drop → `restoreIntoNewPanel`.
 *
 * Used by both `EntityLink` (whose whole root is draggable) and
 * `DraggableEntityHandle` (a grip inside a FullPanel).
 */

import type { FxtrID } from "@fxtr/core";
import type { DragEvent as ReactDragEvent } from "react";
import { useCallback } from "react";
import { useBreadcrumbSource } from "../breadcrumb/BreadcrumbSourceContext.js";
import { computeSnapshotFromTrail } from "../breadcrumb/history.js";
import { buildTrail } from "../breadcrumb/types.js";
import { usePanelContext } from "../workstation/PanelContext.js";
import { type DragPayload, setDragData } from "./drag.js";

export interface UseDraggableEntityResult {
	draggable: true;
	onDragStart: (e: ReactDragEvent) => void;
}

export function useDraggableEntity(entity: FxtrID): UseDraggableEntityResult {
	const source = useBreadcrumbSource();
	const panel = usePanelContext();

	const onDragStart = useCallback(
		(e: ReactDragEvent) => {
			if (!e.dataTransfer) return;
			const trail = buildTrail(source, entity);
			const payload: DragPayload = panel
				? {
						kind: "link",
						entityIdString: entity.idString(),
						trail,
						sourcePanelId: panel.panelId,
					}
				: {
						kind: "snapshot",
						entityIdString: entity.idString(),
						newPanelState: computeSnapshotFromTrail(trail),
					};
			setDragData(e.dataTransfer, payload);
		},
		[entity, source, panel],
	);

	return { draggable: true, onDragStart };
}

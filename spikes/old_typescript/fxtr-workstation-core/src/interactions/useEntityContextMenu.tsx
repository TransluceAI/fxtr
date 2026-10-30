/**
 * useEntityContextMenu — right-click menu wrapper for a content ref.
 *
 * Returns a `Wrap` component that shows a small menu on right-click.
 * One item for now: "Open in new panel". Branches on `usePanelContext`:
 *
 * - Inside a panel: `followLink({ ..., intent: { kind: "open-new" } })`.
 *   The shell derives the new chain from the trail and captures the
 *   source panel's state as historyRoot.
 * - Outside a panel: `restoreIntoNewPanel(computeSnapshotFromTrail(...))`.
 *
 * The "context-menu" via stamp is used for the final trail segment so
 * breadcrumbs can reflect the user's explicit gesture if we ever add
 * a visible trail.
 */

import { ContextMenu } from "@base-ui/react/context-menu";
import type { FxtrID } from "@fxtr/core";
import { type ComponentType, type ReactNode, useCallback, useMemo } from "react";
import { useBreadcrumbSource } from "../breadcrumb/BreadcrumbSourceContext.js";
import { computeSnapshotFromTrail } from "../breadcrumb/history.js";
import type { Breadcrumb } from "../breadcrumb/types.js";
import { usePanelContext } from "../workstation/PanelContext.js";
import { useWorkstation } from "../workstation/WorkstationContext.js";
import { useCloseAllHovers } from "./HoverContext.js";

export interface ContextMenuWrapProps {
	children: ReactNode;
}

export function useEntityContextMenu(entity: FxtrID): ComponentType<ContextMenuWrapProps> {
	const source = useBreadcrumbSource();
	const panel = usePanelContext();
	const { followLink, restoreIntoNewPanel } = useWorkstation();
	const closeAllHovers = useCloseAllHovers();

	const openInPanel = useCallback(() => {
		closeAllHovers();
		const targetIdString = entity.idString();
		// Build a trail with an explicit "context-menu" via stamp on the
		// final segment so breadcrumbs can reflect the user's gesture.
		const finalSegment: Breadcrumb =
			source && source.currentKey !== null
				? {
						fromEntityId: source.currentKey.idString(),
						toEntityId: targetIdString,
						via: "context-menu",
					}
				: { toEntityId: targetIdString, via: "context-menu" };
		const pending = source?.pendingBreadcrumbs ?? [];
		const trail: Breadcrumb[] = [...pending, finalSegment];

		if (panel) {
			followLink({
				target: entity,
				trail,
				sourcePanelId: panel.panelId,
				intent: { kind: "open-new" },
			});
		} else {
			restoreIntoNewPanel(computeSnapshotFromTrail(trail));
		}
	}, [entity, source, panel, followLink, restoreIntoNewPanel, closeAllHovers]);

	return useMemo(() => makeContextMenuWrap(entity, openInPanel), [entity, openInPanel]);
}

function makeContextMenuWrap(
	entity: FxtrID,
	openInPanel: () => void,
): ComponentType<ContextMenuWrapProps> {
	function ContextMenuWrap({ children }: ContextMenuWrapProps) {
		return (
			<ContextMenu.Root>
				<ContextMenu.Trigger render={<span style={{ display: "contents" }} />}>
					{children}
				</ContextMenu.Trigger>
				<ContextMenu.Portal>
					<ContextMenu.Positioner>
						<ContextMenu.Popup className="fw-context-menu-popup">
							<ContextMenu.Item className="fw-context-menu-item" onClick={openInPanel}>
								Open in new panel
							</ContextMenu.Item>
						</ContextMenu.Popup>
					</ContextMenu.Positioner>
				</ContextMenu.Portal>
			</ContextMenu.Root>
		);
	}
	ContextMenuWrap.displayName = `ContextMenuWrap(${entity.idString()})`;
	return ContextMenuWrap;
}

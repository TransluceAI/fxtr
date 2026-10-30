/**
 * useRefContextMenu — right-click menu wrapper for a content
 * ref (entity or service).
 *
 * Up to four menu items depending on whether the click originated
 * inside a ref panel or not:
 *
 *   - "Open in new panel" — always available; default view in a
 *     new panel.
 *   - "Open metadata in new panel" — always available; hardcoded
 *     metadata view in a new panel.
 *   - "Open here" — only inside a panel; replace-in-source with
 *     default view.
 *   - "Open metadata here" — only inside a panel; replace-in-
 *     source with metadata view.
 *
 * Open-in-new-panel items route through `followLink` (with a
 * source panel) or `restoreIntoNewPanel` (no source).
 * Open-here items always route through `followLink` with
 * `replace-in-source` intent.
 *
 * The "context-menu" via stamp is used for the final trail
 * segment so breadcrumbs can reflect the user's explicit gesture
 * if we ever add a visible trail.
 */

import { ContextMenu } from "@base-ui/react/context-menu";
import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import { type ComponentType, type ReactNode, useCallback, useMemo } from "react";
import { useBreadcrumbSource } from "../breadcrumb/BreadcrumbSourceContext.js";
import { computeSnapshotFromTrail } from "../breadcrumb/history.js";
import type { Breadcrumb } from "../breadcrumb/types.js";
import { usePanelContext } from "../panel/PanelContext.js";
import { usePanelViewer } from "../shell/PanelViewerContext.js";
import { type PanelViewDisplay, entryKind, panelViewEntryOf } from "../types.js";
import { useCloseAllHovers } from "./HoverContext.js";

export interface ContextMenuWrapProps {
	readonly children: ReactNode;
}

interface MenuActions {
	readonly openInNewPanel: () => void;
	readonly openMetadataInNewPanel: () => void;
	readonly openHere: (() => void) | null;
	readonly openMetadataHere: (() => void) | null;
}

export function useRefContextMenu(
	stub: EntityRefStub | ServiceRefStub,
): ComponentType<ContextMenuWrapProps> {
	const source = useBreadcrumbSource();
	const panel = usePanelContext();
	const { followLink, restoreIntoNewPanel } = usePanelViewer();
	const closeAllHovers = useCloseAllHovers();

	const openWith = useCallback(
		(display: PanelViewDisplay, intent: "open-new" | "replace-in-source") => {
			closeAllHovers();
			const target = { ...panelViewEntryOf(stub), display };
			const finalSegment: Breadcrumb =
				source !== null && source.currentKey !== null
					? { from: source.currentKey, to: target, via: "context-menu" }
					: { to: target, via: "context-menu" };
			const pending = source?.pendingBreadcrumbs ?? [];
			const trail: Breadcrumb[] = [...pending, finalSegment];

			if (intent === "replace-in-source") {
				// `replace-in-source` requires a source panel.
				if (panel === null) return;
				followLink({
					target,
					trail,
					sourcePanelId: panel.panelId,
					intent: { kind: "replace-in-source" },
				});
				return;
			}
			// `open-new`: prefer the source-panel-aware path for
			// history-root capture; fall back to a snapshot when
			// there's no source panel.
			if (panel !== null) {
				followLink({
					target,
					trail,
					sourcePanelId: panel.panelId,
					intent: { kind: "open-new" },
				});
			} else {
				restoreIntoNewPanel(computeSnapshotFromTrail(trail));
			}
		},
		[stub, source, panel, followLink, restoreIntoNewPanel, closeAllHovers],
	);

	const actions = useMemo<MenuActions>(
		() => ({
			openInNewPanel: () => openWith("default", "open-new"),
			openMetadataInNewPanel: () => openWith("metadata", "open-new"),
			openHere: panel !== null ? () => openWith("default", "replace-in-source") : null,
			openMetadataHere: panel !== null ? () => openWith("metadata", "replace-in-source") : null,
		}),
		[openWith, panel],
	);

	return useMemo(() => makeContextMenuWrap(stub, actions), [stub, actions]);
}

function makeContextMenuWrap(
	stub: EntityRefStub | ServiceRefStub,
	actions: MenuActions,
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
							{actions.openHere !== null && (
								<ContextMenu.Item className="fw-context-menu-item" onClick={actions.openHere}>
									Open here
								</ContextMenu.Item>
							)}
							<ContextMenu.Item className="fw-context-menu-item" onClick={actions.openInNewPanel}>
								Open in new panel
							</ContextMenu.Item>
							{actions.openMetadataHere !== null && (
								<ContextMenu.Item
									className="fw-context-menu-item"
									onClick={actions.openMetadataHere}
								>
									Open metadata here
								</ContextMenu.Item>
							)}
							<ContextMenu.Item
								className="fw-context-menu-item"
								onClick={actions.openMetadataInNewPanel}
							>
								Open metadata in new panel
							</ContextMenu.Item>
						</ContextMenu.Popup>
					</ContextMenu.Positioner>
				</ContextMenu.Portal>
			</ContextMenu.Root>
		);
	}
	const kind = entryKind(panelViewEntryOf(stub));
	ContextMenuWrap.displayName = `ContextMenuWrap(${kind}:${stub.shortId})`;
	return ContextMenuWrap;
}

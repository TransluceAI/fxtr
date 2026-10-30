/**
 * useRefClick — click handler for a content ref (entity or
 * service).
 *
 * Branches on whether the click originated inside a ref panel:
 *
 *   - Inside a panel (`usePanelContext() !== null`): calls
 *     `followLink({ ..., intent: { kind: "replace-in-source" } })`.
 *     The shell mutates the source panel's chain via
 *     `computeNextChainState`.
 *   - Outside a panel (e.g. a click inside a roots-panel row or a
 *     hover card rooted in one): calls
 *     `restoreIntoNewPanel(computeSnapshotFromTrail(trail))`. No
 *     panel to mutate; a fresh chain state is derived from the
 *     trail.
 *
 * Before calling either, we close all hover popups so stale
 * preview cards don't stick around after the user has moved on.
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import type { MouseEvent as ReactMouseEvent } from "react";
import { useCallback } from "react";
import { useBreadcrumbSource } from "../breadcrumb/BreadcrumbSourceContext.js";
import { computeSnapshotFromTrail } from "../breadcrumb/history.js";
import { buildTrail } from "../breadcrumb/types.js";
import { usePanelContext } from "../panel/PanelContext.js";
import { usePanelViewer } from "../shell/PanelViewerContext.js";
import { panelViewEntryOf } from "../types.js";
import { useCloseAllHovers } from "./HoverContext.js";

export interface UseRefClickResult {
	readonly onClick: (e: ReactMouseEvent) => void;
}

export function useRefClick(stub: EntityRefStub | ServiceRefStub): UseRefClickResult {
	const source = useBreadcrumbSource();
	const panel = usePanelContext();
	const { followLink, restoreIntoNewPanel } = usePanelViewer();
	const closeAllHovers = useCloseAllHovers();

	const onClick = useCallback(
		(e: ReactMouseEvent) => {
			e.preventDefault();
			e.stopPropagation();
			closeAllHovers();
			const target = panelViewEntryOf(stub);
			const trail = buildTrail(source, target);
			if (panel !== null) {
				followLink({
					target,
					trail,
					sourcePanelId: panel.panelId,
					intent: { kind: "replace-in-source" },
				});
			} else {
				restoreIntoNewPanel(computeSnapshotFromTrail(trail));
			}
		},
		[stub, source, panel, followLink, restoreIntoNewPanel, closeAllHovers],
	);

	return { onClick };
}

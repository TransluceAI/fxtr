/**
 * useEntityClick — click handler for a content entity ref.
 *
 * Branches on whether the click originated inside an entity panel:
 *
 * - Inside a panel (`usePanelContext() !== null`): calls
 *   `followLink({ ..., intent: { kind: "replace-in-source" } })`. The
 *   shell mutates the source panel's chain via
 *   `computeNextChainState`.
 * - Outside a panel (e.g. a click inside a namespace-pin row or a
 *   hover card rooted in one): calls
 *   `restoreIntoNewPanel(computeSnapshotFromTrail(trail))`. No panel
 *   to mutate; a fresh snapshot is derived from the trail.
 *
 * Before calling either, we close all hover popups so stale preview
 * cards don't stick around after the user has moved on.
 */

import type { FxtrID } from "@fxtr/core";
import type { MouseEvent as ReactMouseEvent } from "react";
import { useCallback } from "react";
import { useBreadcrumbSource } from "../breadcrumb/BreadcrumbSourceContext.js";
import { computeSnapshotFromTrail } from "../breadcrumb/history.js";
import { buildTrail } from "../breadcrumb/types.js";
import { usePanelContext } from "../workstation/PanelContext.js";
import { useWorkstation } from "../workstation/WorkstationContext.js";
import { useCloseAllHovers } from "./HoverContext.js";

export interface UseEntityClickResult {
	onClick: (e: ReactMouseEvent) => void;
}

export function useEntityClick(entity: FxtrID): UseEntityClickResult {
	const source = useBreadcrumbSource();
	const panel = usePanelContext();
	const { followLink, restoreIntoNewPanel } = useWorkstation();
	const closeAllHovers = useCloseAllHovers();

	const onClick = useCallback(
		(e: ReactMouseEvent) => {
			e.preventDefault();
			e.stopPropagation();
			closeAllHovers();
			const trail = buildTrail(source, entity);
			if (panel) {
				followLink({
					target: entity,
					trail,
					sourcePanelId: panel.panelId,
					intent: { kind: "replace-in-source" },
				});
			} else {
				restoreIntoNewPanel(computeSnapshotFromTrail(trail));
			}
		},
		[entity, source, panel, followLink, restoreIntoNewPanel, closeAllHovers],
	);

	return { onClick };
}

/**
 * BreadcrumbEntry — a single entry in the chain row inside
 * `BreadcrumbBar`. Composes the interaction hooks but overrides click:
 *
 *   - click         → `activateChainEntry(panelId, chainIndex)`
 *                     (shifts active position; no chain mutation)
 *   - hover         → HoverPreview card (via `useEntityHoverCard`)
 *   - right-click   → "Open in new panel" via `restoreIntoNewPanel`
 *                     + `computeOpenNewFromChainEntry`
 *   - drag          → SnapshotDragPayload (new hook
 *                     `useDraggableChainEntry`); drop →
 *                     `restoreIntoNewPanel`
 */

import { ContextMenu } from "@base-ui/react/context-menu";
import { FxtrID } from "@fxtr/core";
import { Render } from "@fxtr/react-dispatch";
import {
	type KeyboardEvent as ReactKeyboardEvent,
	type MouseEvent as ReactMouseEvent,
	Suspense,
	useCallback,
	useMemo,
} from "react";
import { useCloseAllHovers } from "../interactions/HoverContext.js";
import { useDraggableChainEntry } from "../interactions/useDraggableChainEntry.js";
import { useEntityHoverCard } from "../interactions/useEntityHoverCard.js";
import { InlineLink } from "../keys.js";
import { shortId } from "../shortId.js";
import { useWorkstation } from "../workstation/WorkstationContext.js";
import { computeOpenNewFromChainEntry } from "./history.js";
import type { EntityPanelParams } from "./types.js";

export interface BreadcrumbEntryProps {
	panelId: string;
	panelState: EntityPanelParams;
	chainIndex: number;
	entityIdString: string;
	isActive: boolean;
	isFuture: boolean;
}

export function BreadcrumbEntry(props: BreadcrumbEntryProps) {
	const { panelId, panelState, chainIndex, entityIdString, isActive, isFuture } = props;
	const { activateChainEntry, restoreIntoNewPanel } = useWorkstation();
	const closeAllHovers = useCloseAllHovers();

	const entity = useMemo(() => FxtrID.fromIdString(entityIdString), [entityIdString]);

	const HoverWrap = useEntityHoverCard(entity, "inline");
	const { draggable, onDragStart } = useDraggableChainEntry({
		sourcePanelId: panelId,
		panelState,
		chainIndex,
	});

	const onClick = useCallback(
		(e: ReactMouseEvent) => {
			e.preventDefault();
			e.stopPropagation();
			closeAllHovers();
			activateChainEntry(panelId, chainIndex);
		},
		[activateChainEntry, closeAllHovers, panelId, chainIndex],
	);

	const onKeyDown = useCallback(
		(e: ReactKeyboardEvent) => {
			if (e.key === "Enter" || e.key === " ") {
				e.preventDefault();
				closeAllHovers();
				activateChainEntry(panelId, chainIndex);
			}
		},
		[activateChainEntry, closeAllHovers, panelId, chainIndex],
	);

	const onOpenInNewPanel = useCallback(() => {
		closeAllHovers();
		const next = computeOpenNewFromChainEntry(panelState, chainIndex);
		restoreIntoNewPanel(next);
	}, [closeAllHovers, panelState, chainIndex, restoreIntoNewPanel]);

	const className = [
		"fw-breadcrumb-entry",
		isActive ? "fw-breadcrumb-entry--active" : "",
		isFuture ? "fw-breadcrumb-entry--future" : "",
	]
		.filter(Boolean)
		.join(" ");

	return (
		<HoverWrap>
			<ContextMenu.Root>
				<ContextMenu.Trigger render={<span style={{ display: "contents" }} />}>
					<span
						className={className}
						role="button"
						tabIndex={0}
						onClick={onClick}
						onKeyDown={onKeyDown}
						draggable={draggable}
						onDragStart={onDragStart}
						title={isActive ? "Active — click to stay" : "Click to jump here"}
					>
						<Suspense fallback={<span>{shortId(entity)}</span>}>
							<Render entity={entity} as={InlineLink} />
						</Suspense>
					</span>
				</ContextMenu.Trigger>
				<ContextMenu.Portal>
					<ContextMenu.Positioner>
						<ContextMenu.Popup className="fw-context-menu-popup">
							<ContextMenu.Item className="fw-context-menu-item" onClick={onOpenInNewPanel}>
								Open in new panel
							</ContextMenu.Item>
						</ContextMenu.Popup>
					</ContextMenu.Positioner>
				</ContextMenu.Portal>
			</ContextMenu.Root>
		</HoverWrap>
	);
}

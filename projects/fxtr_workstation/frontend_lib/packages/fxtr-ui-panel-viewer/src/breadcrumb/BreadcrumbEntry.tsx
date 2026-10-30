/**
 * BreadcrumbEntry — a single entry in the chain row inside
 * `BreadcrumbBar`. Composes the interaction hooks but overrides click:
 *
 *   - click         → `activateChainEntry(panelId, chainIndex)`
 *                     (shifts active position; no chain mutation)
 *   - hover         → HoverPreview card (via `useRefHoverCard`)
 *   - right-click   → "Open in new panel" via `restoreIntoNewPanel`
 *                     + `computeOpenNewFromChainEntry`
 *   - drag          → SnapshotDragPayload (via
 *                     `useDraggableChainEntry`); drop →
 *                     `restoreIntoNewPanel`
 */

import { ContextMenu } from "@base-ui/react/context-menu";
import { RenderDispatch } from "@fxtr-ui/react-dispatch";
import { InlineLink } from "@fxtr-ui/workstation-dispatch";
import {
	type KeyboardEvent as ReactKeyboardEvent,
	type MouseEvent as ReactMouseEvent,
	Suspense,
	useCallback,
} from "react";
import { useCloseAllHovers } from "../reflink/HoverContext.js";
import { useDraggableChainEntry } from "../reflink/useDraggableChainEntry.js";
import { useRefHoverCard } from "../reflink/useRefHoverCard.js";
import { usePanelViewer } from "../shell/PanelViewerContext.js";
import type { PanelViewEntry } from "../types.js";
import { computeOpenNewFromChainEntry } from "./history.js";
import type { RefPanelChainState } from "./types.js";

export interface BreadcrumbEntryProps {
	readonly panelId: string;
	readonly panelState: RefPanelChainState;
	readonly chainIndex: number;
	readonly ref: PanelViewEntry;
	readonly isActive: boolean;
	readonly isFuture: boolean;
}

export function BreadcrumbEntry(props: BreadcrumbEntryProps) {
	const { panelId, panelState, chainIndex, ref, isActive, isFuture } = props;
	const { activateChainEntry, restoreIntoNewPanel } = usePanelViewer();
	const closeAllHovers = useCloseAllHovers();

	// `ref.stub` is already the connector-interned instance; no
	// reconstruction needed (and we deliberately avoid taking a
	// connector dependency here so future memoization stays narrow).
	const stub = ref.stub;

	const HoverWrap = useRefHoverCard(stub, "inline");
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

	const onOpenMetadataInNewPanel = useCallback(() => {
		closeAllHovers();
		// Same rewound chain, but swap the active entry's display
		// mode to "metadata" so the new panel renders the metadata
		// view instead of dispatching FullPanel.
		const next = computeOpenNewFromChainEntry(panelState, chainIndex);
		const replaced = [...next.chain];
		replaced[next.activeIndex] = { ...ref, display: "metadata" };
		restoreIntoNewPanel({ ...next, chain: replaced });
	}, [closeAllHovers, panelState, chainIndex, ref, restoreIntoNewPanel]);

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
						{ref.display === "metadata" ? (
							<span className="fw-metadata-bubble">
								<span className="fw-metadata-bubble__label">Metadata</span>
								<Suspense fallback={<span>{stub.shortId}</span>}>
									<RenderDispatch stub={stub} as={InlineLink} />
								</Suspense>
							</span>
						) : (
							<Suspense fallback={<span>{stub.shortId}</span>}>
								<RenderDispatch stub={stub} as={InlineLink} />
							</Suspense>
						)}
					</span>
				</ContextMenu.Trigger>
				<ContextMenu.Portal>
					<ContextMenu.Positioner>
						<ContextMenu.Popup className="fw-context-menu-popup">
							<ContextMenu.Item className="fw-context-menu-item" onClick={onOpenInNewPanel}>
								Open in new panel
							</ContextMenu.Item>
							<ContextMenu.Item className="fw-context-menu-item" onClick={onOpenMetadataInNewPanel}>
								Open metadata in new panel
							</ContextMenu.Item>
						</ContextMenu.Popup>
					</ContextMenu.Positioner>
				</ContextMenu.Portal>
			</ContextMenu.Root>
		</HoverWrap>
	);
}

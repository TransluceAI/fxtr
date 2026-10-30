/**
 * `HistoryRootEntry` — a single item inside `HistoryRootMenu`.
 * Click and Enter/Space activate
 * `restoreIntoNewPanel(computeOpenNewFromHistoryEntry(entry))`;
 * drag emits a `SnapshotDragPayload` with the same computed state.
 * Hover surfaces a preview card via `useRefHoverCard`. No
 * right-click — nested context menus inside a popup are
 * confusing. The label dispatches through `InlineLink`; a
 * Suspense fallback shows the ref's `shortId` while loading.
 */

import { RenderDispatch } from "@fxtr-ui/react-dispatch";
import { InlineLink } from "@fxtr-ui/workstation-dispatch";
import { type KeyboardEvent as ReactKeyboardEvent, Suspense, useCallback, useMemo } from "react";
import { useCloseAllHovers } from "../reflink/HoverContext.js";
import { useDraggableHistoryEntry } from "../reflink/useDraggableHistoryEntry.js";
import { useRefHoverCard } from "../reflink/useRefHoverCard.js";
import { usePanelViewer } from "../shell/PanelViewerContext.js";
import { type FlatHistoryEntry, computeOpenNewFromHistoryEntry } from "./history.js";

export interface HistoryRootEntryProps {
	readonly entry: FlatHistoryEntry;
	/** For drag-payload attribution. */
	readonly sourcePanelId?: string | undefined;
	/** Called after the entry triggers a restore — lets the menu close. */
	readonly onActivate?: (() => void) | undefined;
}

export function HistoryRootEntry(props: HistoryRootEntryProps) {
	const { entry, sourcePanelId, onActivate } = props;
	const { restoreIntoNewPanel } = usePanelViewer();
	const closeAllHovers = useCloseAllHovers();

	// `entry.ref.stub` is the connector-interned instance — no
	// reconstruction needed.
	const stub = entry.ref.stub;

	const HoverWrap = useRefHoverCard(stub, "inline");
	const dragArgs = useMemo(
		() => ({ entry, ...(sourcePanelId !== undefined ? { sourcePanelId } : {}) }),
		[entry, sourcePanelId],
	);
	const { draggable, onDragStart } = useDraggableHistoryEntry(dragArgs);

	const onClick = useCallback(() => {
		closeAllHovers();
		const next = computeOpenNewFromHistoryEntry(entry);
		restoreIntoNewPanel(next);
		onActivate?.();
	}, [closeAllHovers, entry, restoreIntoNewPanel, onActivate]);

	const onKeyDown = useCallback(
		(e: ReactKeyboardEvent) => {
			if (e.key === "Enter" || e.key === " ") {
				e.preventDefault();
				closeAllHovers();
				const next = computeOpenNewFromHistoryEntry(entry);
				restoreIntoNewPanel(next);
				onActivate?.();
			}
		},
		[closeAllHovers, entry, restoreIntoNewPanel, onActivate],
	);

	return (
		<HoverWrap>
			<div
				className="fw-history-root-entry"
				role="button"
				tabIndex={0}
				onClick={onClick}
				onKeyDown={onKeyDown}
				draggable={draggable}
				onDragStart={onDragStart}
				title="Click or drag to reopen this state"
			>
				{entry.ref.display === "metadata" ? (
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
			</div>
		</HoverWrap>
	);
}

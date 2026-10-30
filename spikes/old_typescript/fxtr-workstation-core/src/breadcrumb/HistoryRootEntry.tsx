/**
 * HistoryRootEntry — a single item inside `HistoryRootMenu`.
 *
 *   - click → `restoreIntoNewPanel(computeOpenNewFromHistoryEntry(entry))`
 *   - drag  → SnapshotDragPayload (same computation)
 *   - hover → HoverPreview
 *
 * No right-click; the popup is already a menu — a nested context menu
 * on top would just be confusing. Entries render their label via
 * `<Render as={InlineLink}>` wrapped in Suspense + shortId fallback.
 */

import { FxtrID } from "@fxtr/core";
import { Render } from "@fxtr/react-dispatch";
import { type KeyboardEvent as ReactKeyboardEvent, Suspense, useCallback, useMemo } from "react";
import { useCloseAllHovers } from "../interactions/HoverContext.js";
import { useDraggableHistoryEntry } from "../interactions/useDraggableHistoryEntry.js";
import { useEntityHoverCard } from "../interactions/useEntityHoverCard.js";
import { InlineLink } from "../keys.js";
import { shortId } from "../shortId.js";
import { useWorkstation } from "../workstation/WorkstationContext.js";
import { type FlatHistoryEntry, computeOpenNewFromHistoryEntry } from "./history.js";

export interface HistoryRootEntryProps {
	entry: FlatHistoryEntry;
	/** For drag-payload attribution. */
	sourcePanelId?: string | undefined;
	/** Called after the entry triggers a restore — lets the menu close. */
	onActivate?: (() => void) | undefined;
}

export function HistoryRootEntry(props: HistoryRootEntryProps) {
	const { entry, sourcePanelId, onActivate } = props;
	const { restoreIntoNewPanel } = useWorkstation();
	const closeAllHovers = useCloseAllHovers();

	const entity = useMemo(() => FxtrID.fromIdString(entry.entityIdString), [entry.entityIdString]);

	const HoverWrap = useEntityHoverCard(entity, "inline");
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
				<Suspense fallback={<span>{shortId(entity)}</span>}>
					<Render entity={entity} as={InlineLink} />
				</Suspense>
			</div>
		</HoverWrap>
	);
}

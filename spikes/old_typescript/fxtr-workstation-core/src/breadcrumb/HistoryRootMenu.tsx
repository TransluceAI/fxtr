/**
 * HistoryRootMenu — base-ui Menu wrapping the breadcrumb bar's history-
 * root trigger button. When open, shows a flat list of every ancestor
 * entity across all historyRoot segments, grouped by segment with
 * thin dividers.
 *
 * `open` / `onOpenChange` are controlled by the parent (BreadcrumbBar)
 * so we can close the popup after the user clicks an entry (the
 * `HistoryRootEntry` calls `onActivate` which flips `open` to false).
 *
 * Empty state (no ancestry; `historyRoot.kind === "namespace"`): a
 * friendly "Opened from the namespace" message. The trigger button
 * stays visible for layout stability.
 */

import { Menu } from "@base-ui/react/menu";
import { type ReactNode, useMemo } from "react";
import { HistoryRootEntry } from "./HistoryRootEntry.js";
import { flattenHistoryRoot } from "./flatten.js";
import type { FlatHistoryEntry } from "./history.js";
import type { HistoryRoot } from "./types.js";

export interface HistoryRootMenuProps {
	historyRoot: HistoryRoot;
	trigger: ReactNode;
	open: boolean;
	onOpenChange: (open: boolean) => void;
	/** For drag-payload attribution on entries. */
	sourcePanelId?: string | undefined;
}

export function HistoryRootMenu(props: HistoryRootMenuProps) {
	const { historyRoot, trigger, open, onOpenChange, sourcePanelId } = props;
	const flat = useMemo(() => flattenHistoryRoot(historyRoot), [historyRoot]);
	const isEmpty = flat.length === 0;

	const close = () => onOpenChange(false);

	return (
		<Menu.Root open={open} onOpenChange={onOpenChange}>
			<Menu.Trigger render={trigger as unknown as React.ReactElement} />
			<Menu.Portal>
				<Menu.Positioner side="bottom" align="start" sideOffset={6}>
					<Menu.Popup className="fw-history-root-menu-popup">
						{isEmpty ? (
							<div className="fw-history-root-menu-popup__empty">Opened from the namespace.</div>
						) : (
							<SegmentedEntries entries={flat} sourcePanelId={sourcePanelId} onActivate={close} />
						)}
					</Menu.Popup>
				</Menu.Positioner>
			</Menu.Portal>
		</Menu.Root>
	);
}

interface SegmentedEntriesProps {
	entries: FlatHistoryEntry[];
	sourcePanelId?: string | undefined;
	onActivate: () => void;
}

function SegmentedEntries({ entries, sourcePanelId, onActivate }: SegmentedEntriesProps) {
	// Group consecutive entries by segmentIndex so we can insert a
	// divider between segments.
	const nodes: ReactNode[] = [];
	let lastSegment = -1;
	for (const entry of entries) {
		if (entry.segmentIndex !== lastSegment) {
			if (lastSegment !== -1) {
				nodes.push(
					<hr
						key={`divider-${entry.segmentIndex}`}
						className="fw-history-root-menu-popup__segment-divider"
					/>,
				);
			}
			lastSegment = entry.segmentIndex;
		}
		nodes.push(
			<HistoryRootEntry
				key={`${entry.segmentIndex}:${entry.positionInSegment}:${entry.entityIdString}`}
				entry={entry}
				sourcePanelId={sourcePanelId}
				onActivate={onActivate}
			/>,
		);
	}
	return <>{nodes}</>;
}

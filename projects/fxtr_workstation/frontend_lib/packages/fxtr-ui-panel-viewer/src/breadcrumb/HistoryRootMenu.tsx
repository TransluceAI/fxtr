/**
 * `HistoryRootMenu` — base-ui Menu wrapping the breadcrumb bar's
 * history-root trigger button. When open, shows a flat list of
 * every ancestor ref across all historyRoot segments, grouped by
 * segment with thin dividers. `open` / `onOpenChange` are
 * controlled by the parent (the `BreadcrumbBar`) so the popup can
 * close after the user clicks an entry — each `HistoryRootEntry`
 * invokes `onActivate` which flips `open` to false. The
 * empty-state body (`historyRoot.kind === "roots"`) shows a
 * friendly "Opened from the roots panel." message; the trigger
 * button stays visible for layout stability.
 */

import { Menu } from "@base-ui/react/menu";
import { type ReactNode, useMemo } from "react";
import { HistoryRootEntry } from "./HistoryRootEntry.js";
import { flattenHistoryRoot } from "./flatten.js";
import type { FlatHistoryEntry } from "./history.js";
import type { HistoryRoot } from "./types.js";

export interface HistoryRootMenuProps {
	readonly historyRoot: HistoryRoot;
	readonly trigger: ReactNode;
	readonly open: boolean;
	readonly onOpenChange: (open: boolean) => void;
	/** For drag-payload attribution on entries. */
	readonly sourcePanelId?: string | undefined;
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
							<div className="fw-history-root-menu-popup__empty">Opened from the roots panel.</div>
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
	readonly entries: ReadonlyArray<FlatHistoryEntry>;
	readonly sourcePanelId?: string | undefined;
	readonly onActivate: () => void;
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
				key={`${entry.segmentIndex}:${entry.positionInSegment}:${entry.ref.stub.shortId}`}
				entry={entry}
				sourcePanelId={sourcePanelId}
				onActivate={onActivate}
			/>,
		);
	}
	return <>{nodes}</>;
}

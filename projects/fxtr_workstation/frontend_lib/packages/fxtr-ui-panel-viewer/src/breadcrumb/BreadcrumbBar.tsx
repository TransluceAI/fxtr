/**
 * BreadcrumbBar — fixed row at the top of each ref panel.
 *
 * Layout:
 *
 *   [◧]  A  ›  B  ›  C  ›  D
 *    ↑       past  active  future (gray)
 *
 * - Left: history-root trigger button → opens `HistoryRootMenu`.
 * - Middle: chain of `<BreadcrumbEntry>`s separated by `›`.
 *   Overflow scrolls horizontally (no visible scrollbar).
 *
 * No logic for history-root emptiness beyond "always render the
 * button" — the menu itself shows a friendly empty-state when there's
 * no ancestry.
 */

import { Fragment, useState } from "react";
import type { PanelViewEntry } from "../types.js";
import { BreadcrumbEntry } from "./BreadcrumbEntry.js";
import { HistoryRootMenu } from "./HistoryRootMenu.js";
import type { HistoryRoot, RefPanelChainState } from "./types.js";

export interface BreadcrumbBarProps {
	readonly panelId: string;
	readonly chain: ReadonlyArray<PanelViewEntry>;
	readonly activeIndex: number;
	readonly historyRoot: HistoryRoot;
}

export function BreadcrumbBar(props: BreadcrumbBarProps) {
	const { panelId, chain, activeIndex, historyRoot } = props;
	// `storages` is required on `RefPanelChainState` but the only
	// consumer of `panelState` from here is `useDraggableChainEntry`
	// → `computeOpenNewFromChainEntry`, which derives a fresh
	// storages array of `undefined`s for the new panel; it doesn't
	// read `source.storages`. An empty array satisfies the type.
	const panelState: RefPanelChainState = {
		chain,
		activeIndex,
		historyRoot,
		storages: [],
	};
	const [menuOpen, setMenuOpen] = useState(false);

	return (
		<div className="fw-breadcrumb-bar">
			<HistoryRootMenu
				historyRoot={historyRoot}
				open={menuOpen}
				onOpenChange={setMenuOpen}
				sourcePanelId={panelId}
				trigger={
					<button
						type="button"
						className="fw-breadcrumb-bar__history-button"
						title="Opened from…"
						aria-label="Show history root"
					>
						{"…"}
					</button>
				}
			/>
			<div className="fw-breadcrumb-bar__chain">
				{chain.map((ref, index) => (
					<Fragment key={`${ref.stub.shortId}:${ref.display}:${index}`}>
						{index > 0 ? (
							<span className="fw-breadcrumb-bar__separator" aria-hidden="true">
								›
							</span>
						) : null}
						<BreadcrumbEntry
							panelId={panelId}
							panelState={panelState}
							chainIndex={index}
							ref={ref}
							isActive={index === activeIndex}
							isFuture={index > activeIndex}
						/>
					</Fragment>
				))}
			</div>
		</div>
	);
}

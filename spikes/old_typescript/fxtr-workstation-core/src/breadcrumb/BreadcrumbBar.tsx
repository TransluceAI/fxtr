/**
 * BreadcrumbBar — fixed row at the top of each entity panel.
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
import { BreadcrumbEntry } from "./BreadcrumbEntry.js";
import { HistoryRootMenu } from "./HistoryRootMenu.js";
import type { EntityPanelParams, HistoryRoot } from "./types.js";

export interface BreadcrumbBarProps {
	panelId: string;
	chain: string[];
	activeIndex: number;
	historyRoot: HistoryRoot;
}

export function BreadcrumbBar(props: BreadcrumbBarProps) {
	const { panelId, chain, activeIndex, historyRoot } = props;
	const panelState: EntityPanelParams = { chain, activeIndex, historyRoot };
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
				{chain.map((idString, index) => (
					<Fragment key={`${index}:${idString}`}>
						{index > 0 ? (
							<span className="fw-breadcrumb-bar__separator" aria-hidden="true">
								›
							</span>
						) : null}
						<BreadcrumbEntry
							panelId={panelId}
							panelState={panelState}
							chainIndex={index}
							entityIdString={idString}
							isActive={index === activeIndex}
							isFuture={index > activeIndex}
						/>
					</Fragment>
				))}
			</div>
		</div>
	);
}

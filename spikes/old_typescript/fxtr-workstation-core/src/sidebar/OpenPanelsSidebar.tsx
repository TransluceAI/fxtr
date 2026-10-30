/**
 * OpenPanelsSidebar — left-rail listing every currently-open dockview
 * panel, grouped by dockview group. Click a row to focus that panel;
 * click the X to close it. Entity panels display through the
 * `BlockLink` dispatch (pure display — no interactions at the ref
 * level; the whole row owns click / close). Non-entity panels show
 * their plain title.
 *
 * The namespace panel's close button is suppressed since it auto-
 * reopens anyway — the X would flash visibly broken.
 *
 * The sidebar also owns the collapse / expand toggle. When collapsed,
 * we render only the toggle button so the row still takes visual
 * space (32px) and the user has somewhere obvious to click to expand.
 */

import { Render } from "@fxtr/react-dispatch";
import type { DockviewApi } from "dockview";
import { useCallback } from "react";
import { BlockLink } from "../keys.js";
import { NAMESPACE_PANEL_ID } from "../panels/namespacePanelComponent.js";
import { SidebarTooltip, SidebarTooltipProvider } from "./SidebarTooltip.js";
import { type SidebarGroup, type SidebarPanel, usePanelLayout } from "./usePanelLayout.js";

export interface OpenPanelsSidebarProps {
	api: DockviewApi | null;
	collapsed: boolean;
	onToggleCollapsed: () => void;
}

export function OpenPanelsSidebar({ api, collapsed, onToggleCollapsed }: OpenPanelsSidebarProps) {
	const groups = usePanelLayout(api);

	const focusPanel = useCallback(
		(panelId: string) => {
			api?.getPanel(panelId)?.api.setActive();
		},
		[api],
	);

	const closePanel = useCallback(
		(panelId: string) => {
			const panel = api?.getPanel(panelId);
			if (panel) api?.removePanel(panel);
		},
		[api],
	);

	const className = collapsed ? "fw-sidebar fw-sidebar--collapsed" : "fw-sidebar fw-sidebar--open";

	if (collapsed) {
		return (
			<aside className={className}>
				<button
					type="button"
					className="fw-sidebar__toggle"
					onClick={onToggleCollapsed}
					title="Expand sidebar"
					aria-label="Expand sidebar"
				>
					›
				</button>
			</aside>
		);
	}

	return (
		<aside className={className}>
			<header className="fw-sidebar__header">
				<span className="fw-sidebar__header-label">Panels</span>
				<button
					type="button"
					className="fw-sidebar__toggle"
					onClick={onToggleCollapsed}
					title="Collapse sidebar"
					aria-label="Collapse sidebar"
				>
					‹
				</button>
			</header>
			<div className="fw-sidebar__body">
				{groups.length === 0 ? (
					<div className="fw-sidebar__empty">No panels open.</div>
				) : (
					<SidebarTooltipProvider>
						{groups.map((group, index) => (
							<GroupSection
								key={group.groupId}
								group={group}
								index={index}
								onFocus={focusPanel}
								onClose={closePanel}
							/>
						))}
					</SidebarTooltipProvider>
				)}
			</div>
		</aside>
	);
}

interface GroupSectionProps {
	group: SidebarGroup;
	index: number;
	onFocus: (panelId: string) => void;
	onClose: (panelId: string) => void;
}

function GroupSection({ group, index, onFocus, onClose }: GroupSectionProps) {
	return (
		<div className="fw-sidebar__group">
			<div className="fw-sidebar__group-header">Group {index + 1}</div>
			{group.panels.map((panel) => (
				<SidebarRow key={panel.panelId} panel={panel} onFocus={onFocus} onClose={onClose} />
			))}
		</div>
	);
}

interface SidebarRowProps {
	panel: SidebarPanel;
	onFocus: (panelId: string) => void;
	onClose: (panelId: string) => void;
}

function SidebarRow({ panel, onFocus, onClose }: SidebarRowProps) {
	const canClose = panel.panelId !== NAMESPACE_PANEL_ID;

	const handleClick = useCallback(() => {
		onFocus(panel.panelId);
	}, [onFocus, panel.panelId]);

	const handleClose = useCallback(
		(e: React.MouseEvent) => {
			// Don't also fire the row's onClick handler.
			e.stopPropagation();
			onClose(panel.panelId);
		},
		[onClose, panel.panelId],
	);

	const handleKeyDown = useCallback(
		(e: React.KeyboardEvent) => {
			if (e.key === "Enter" || e.key === " ") {
				e.preventDefault();
				onFocus(panel.panelId);
			}
		},
		[onFocus, panel.panelId],
	);

	const rowClass = `fw-sidebar__row${panel.isActive ? " fw-sidebar__row--active" : ""}`;

	const label = panel.entity ? (
		<SidebarTooltip entity={panel.entity}>
			<span className="fw-sidebar__row-label">
				<Render entity={panel.entity} as={BlockLink} />
			</span>
		</SidebarTooltip>
	) : (
		<span className="fw-sidebar__row-label fw-sidebar__row-label--text">{panel.title}</span>
	);

	return (
		<div
			className={rowClass}
			role="button"
			tabIndex={0}
			onClick={handleClick}
			onKeyDown={handleKeyDown}
			aria-current={panel.isActive ? "true" : undefined}
		>
			{label}
			{canClose ? (
				<button
					type="button"
					className="fw-sidebar__row-close"
					onClick={handleClose}
					title="Close panel"
					aria-label={`Close ${panel.title}`}
				>
					×
				</button>
			) : null}
		</div>
	);
}

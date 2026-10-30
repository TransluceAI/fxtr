/**
 * `OpenPanelsSidebar` — left-rail listing every currently-open
 * dockview panel, grouped by dockview group. Click a row to
 * focus that panel; click the X to close it. Ref panels display
 * through `BlockLink` dispatch on their active stub (pure
 * display — no ref-level interactions; the row owns click /
 * close). Info panels (e.g. roots) show their plain title.
 *
 * Each ref-panel row reads its active stub via
 * `PanelSessionRegistry`: looks up the panel's live session,
 * subscribes to its `userState`, derives the active stub from
 * `chain[activeIndex]`. Pending / unavailable / mid-attach
 * panels render the plain title.
 *
 * The sidebar also owns the collapse / expand toggle. When
 * collapsed, only the toggle button is rendered so the rail
 * still takes visual space (32px) and the user has somewhere
 * obvious to click to expand.
 */

import { RenderDispatch } from "@fxtr-ui/react-dispatch";
import { BlockLink } from "@fxtr-ui/workstation-dispatch";
import type { DockviewApi } from "dockview";
import { type Atom, atom, useAtomValue } from "jotai";
import { type ReactNode, useCallback, useMemo } from "react";
import type { RefPanelChainState } from "../breadcrumb/types.js";
import { REF_PANEL_COMPONENT } from "../panel/RefPanelParams.js";
import { usePanelSessionRegistry } from "../shell/PanelSessionRegistry.js";
import { usePanelViewer } from "../shell/PanelViewerContext.js";
import { SidebarTooltip, SidebarTooltipProvider } from "./SidebarTooltip.js";
import { type SidebarGroup, type SidebarPanel, usePanelLayout } from "./usePanelLayout.js";

/**
 * A header find-or-open button, one per panel registration that
 * declares a `sidebarButton`. The viewer builds these from the
 * injected registrations; the sidebar just renders them.
 */
export interface SidebarButton {
	/** Registration id (used as the React key). */
	readonly id: string;
	/** Button label, rendered as `+ <label>`. */
	readonly label: string;
	/** Find-or-open the registration's panel. */
	readonly onClick: () => void;
}

export interface OpenPanelsSidebarProps {
	readonly api: DockviewApi | null;
	readonly collapsed: boolean;
	readonly onToggleCollapsed: () => void;
	/**
	 * Header find-or-open buttons, one per panel registration that
	 * declares a `sidebarButton`. Provided by `PanelViewer`;
	 * absent (or empty) in test mounts, where the header shows no
	 * action buttons.
	 */
	readonly sidebarButtons?: readonly SidebarButton[];
}

export function OpenPanelsSidebar({
	api,
	collapsed,
	onToggleCollapsed,
	sidebarButtons,
}: OpenPanelsSidebarProps) {
	const groups = usePanelLayout(api);

	const focusPanel = useCallback(
		(panelId: string) => {
			api?.getPanel(panelId)?.api.setActive();
		},
		[api],
	);

	const workstation = usePanelViewer();
	const closePanel = useCallback(
		(panelId: string) => {
			// Route through `workstation.closePanel` so ref-panel
			// sessions get terminated server-side as part of close —
			// same path as clicking the tab's X.
			workstation.closePanel(panelId);
		},
		[workstation],
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
				{(sidebarButtons ?? []).map((button) => (
					<button
						key={button.id}
						type="button"
						className="fw-sidebar__header-action"
						onClick={button.onClick}
						title={`Open the ${button.label.toLowerCase()} panel`}
						aria-label={`Open the ${button.label.toLowerCase()} panel`}
					>
						+ {button.label}
					</button>
				))}
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
	readonly group: SidebarGroup;
	readonly index: number;
	readonly onFocus: (panelId: string) => void;
	readonly onClose: (panelId: string) => void;
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
	readonly panel: SidebarPanel;
	readonly onFocus: (panelId: string) => void;
	readonly onClose: (panelId: string) => void;
}

/**
 * Switch on panel kind. Ref panels go through the session-aware
 * `RefSidebarRow` which dispatches on their active stub; all
 * other panel kinds use the plain `TitleSidebarRow` that shows
 * `panel.title`.
 */
function SidebarRow(props: SidebarRowProps) {
	if (props.panel.component === REF_PANEL_COMPONENT) {
		return <RefSidebarRow {...props} />;
	}
	return <TitleSidebarRow {...props} />;
}

/**
 * Shared row chrome (click / close / active styling). The
 * `label` slot is what differs between ref + title variants.
 */
function RowFrame({
	panel,
	onFocus,
	onClose,
	label,
}: SidebarRowProps & { readonly label: ReactNode }) {
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
			<button
				type="button"
				className="fw-sidebar__row-close"
				onClick={handleClose}
				title="Close panel"
				aria-label={`Close ${panel.title}`}
			>
				×
			</button>
		</div>
	);
}

function TitleSidebarRow(props: SidebarRowProps) {
	const label = (
		<span className="fw-sidebar__row-label fw-sidebar__row-label--text">{props.panel.title}</span>
	);
	return <RowFrame {...props} label={label} />;
}

/**
 * Module-level stable fallback atom for rows whose panel has no
 * registered session (pending / unavailable / no registry in
 * scope). Returning `null` collapses the row to the plain-title
 * fallback inside `RefSidebarRow`.
 */
const NULL_USER_STATE_ATOM: Atom<RefPanelChainState | null> = atom(null);

function RefSidebarRow(props: SidebarRowProps) {
	const registry = usePanelSessionRegistry();

	// Subscribe to the registry's session map so this row
	// re-renders when its panel's session is registered or
	// unregistered. When there's no registry in scope (test
	// mounts), fall back to the empty map atom and treat
	// "session missing" as the steady state.
	const sessions = useAtomValue(registry?.sessions ?? EMPTY_SESSIONS_ATOM);
	const session = sessions.get(props.panel.panelId);

	// Subscribe to the panel's session userState (or a stable
	// null fallback if no session). The cast threads
	// `RefPanelChainState` through the underlying
	// `UserState | null` type.
	const userStateAtom = useMemo(
		() => (session?.userState ?? NULL_USER_STATE_ATOM) as Atom<RefPanelChainState | null>,
		[session],
	);
	const userState = useAtomValue(userStateAtom);
	const activeEntry = userState?.chain[userState.activeIndex];

	// Chain entries carry the live stub directly.
	const concreteStub = activeEntry?.stub ?? null;

	if (concreteStub === null || activeEntry === undefined) {
		// Panel is pending / unavailable / chain not seeded yet —
		// fall through to the title-only row.
		return <TitleSidebarRow {...props} />;
	}

	const isMetadata = activeEntry.display === "metadata";
	const label = (
		<SidebarTooltip stub={concreteStub} isMetadata={isMetadata}>
			<span className="fw-sidebar__row-label">
				{isMetadata ? (
					<span className="fw-metadata-bubble">
						<span className="fw-metadata-bubble__label">Metadata</span>
						<RenderDispatch stub={concreteStub} as={BlockLink} />
					</span>
				) : (
					<RenderDispatch stub={concreteStub} as={BlockLink} />
				)}
			</span>
		</SidebarTooltip>
	);
	return <RowFrame {...props} label={label} />;
}

// Module-level stable atom for the no-registry fallback. `Map`s
// are reference-stable here so `useAtomValue` doesn't re-render
// when the registry is unavailable.
const EMPTY_SESSIONS_ATOM: Atom<ReadonlyMap<string, never>> = atom(new Map<string, never>());

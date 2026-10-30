/**
 * Shell-side wiring for the two `@fxtr-ui/workstation-dispatch`
 * indirections:
 *
 *   - `RefLinkImplContext` — the `<RefLink>` render impl; the
 *     shell wraps the dispatched ref content with drag handlers,
 *     click navigation, hover preview, context menu.
 *
 *   - `WorkstationNavigateContext` — a hook returning a
 *     `(stub) => void` action that consumers (renderers, figures,
 *     etc.) call to navigate the workstation to a stub. Shares
 *     navigation semantics with the ref-link click path:
 *     `followLink({ replace-in-source })` inside a panel,
 *     `restoreIntoNewPanel` otherwise.
 *
 * Both indirections rely on shell-internal contexts
 * (`PanelContext`, `BreadcrumbSourceContext`, `PanelViewerContext`,
 * `HoverCloseContext`) — none of which exist in
 * `@fxtr-ui/workstation-dispatch`, which is why this wiring lives
 * here.
 *
 * `<ShellRefLinkProvider>` installs both providers in one place,
 * mounted once near the top of `<PanelViewer>`'s tree. The
 * navigate impl is a hook (`useShellNavigate`) rather than a
 * concrete function: the consumer's hook scope re-evaluates the
 * relevant contexts on each call site, so one mount serves every
 * position in the tree.
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import { RenderDispatch } from "@fxtr-ui/react-dispatch";
import {
	BlockLink,
	InlineLink,
	RefDropRegionProvider,
	type RefLinkImpl,
	RefLinkImplContext,
	type UseWorkstationNavigate,
	WorkstationNavigateContext,
} from "@fxtr-ui/workstation-dispatch";
import { type CSSProperties, type ReactNode, useCallback } from "react";
import { useBreadcrumbSource } from "../breadcrumb/BreadcrumbSourceContext.js";
import { computeSnapshotFromTrail } from "../breadcrumb/history.js";
import { buildTrail } from "../breadcrumb/types.js";
import { usePanelContext } from "../panel/PanelContext.js";
import { usePanelViewer } from "../shell/PanelViewerContext.js";
import { panelViewEntryOf } from "../types.js";
import { useCloseAllHovers } from "./HoverContext.js";
import { ShellRefStubResolverProvider } from "./ShellRefStubResolver.js";
import { useDraggableRef } from "./useDraggableRef.js";
import { useRefClick } from "./useRefClick.js";
import { useRefContextMenu } from "./useRefContextMenu.js";
import { useRefHoverCard } from "./useRefHoverCard.js";

const TITLE = "Click to navigate, right-click for menu, drag to open in new panel";

const BLOCK_STYLE: CSSProperties = {
	cursor: "pointer",
	userSelect: "none",
};

const INLINE_STYLE: CSSProperties = {
	cursor: "pointer",
	userSelect: "none",
};

/**
 * The shell's `RefLinkImpl`. Calling shape matches
 * `RefLinkImpl = (props: RefLinkProps) => ReactNode`.
 *
 * Composes:
 *   - `<RenderDispatch stub={stub} as={BlockLink | InlineLink}>`
 *     for the display content (dispatched through the registry).
 *   - Four interaction hooks (hover preview, context menu, click,
 *     drag) around that content.
 *
 * `as="inline"` renders a `<span>` so the link flows with
 * surrounding prose; `as="block"` renders a `<div>` that fills
 * its row.
 */
export const ShellRefLinkImpl: RefLinkImpl = (props) => {
	const { stub, as = "block", ...linkProps } = props;

	const HoverWrap = useRefHoverCard(stub, as);
	const ContextMenuWrap = useRefContextMenu(stub);
	const { onClick } = useRefClick(stub);
	const { draggable, onDragStart } = useDraggableRef(stub);

	const key = as === "inline" ? InlineLink : BlockLink;
	const commonProps = {
		onClick,
		draggable,
		onDragStart,
		title: TITLE,
	};

	const content = <RenderDispatch stub={stub} as={key} {...linkProps} />;

	const inner =
		as === "inline" ? (
			<span {...commonProps} style={INLINE_STYLE}>
				{content}
			</span>
		) : (
			<div {...commonProps} style={BLOCK_STYLE}>
				{content}
			</div>
		);

	return (
		<HoverWrap>
			<ContextMenuWrap>{inner}</ContextMenuWrap>
		</HoverWrap>
	);
};

/**
 * Shell-side `UseWorkstationNavigate` impl. Reads the same
 * shell-internal contexts `useRefClick` uses and returns a
 * navigation function with matching semantics.
 *
 * Identity-stable (module-level reference) — calling
 * `useWorkstationNavigate()` from a consumer component results
 * in a consistent inner hook sequence each render, so rules of
 * hooks are satisfied.
 */
export const useShellNavigate: UseWorkstationNavigate = () => {
	const source = useBreadcrumbSource();
	const panel = usePanelContext();
	const { followLink, restoreIntoNewPanel } = usePanelViewer();
	const closeAllHovers = useCloseAllHovers();
	return useCallback(
		(stub: EntityRefStub | ServiceRefStub) => {
			closeAllHovers();
			const target = panelViewEntryOf(stub);
			const trail = buildTrail(source, target);
			if (panel !== null) {
				followLink({
					target,
					trail,
					sourcePanelId: panel.panelId,
					intent: { kind: "replace-in-source" },
				});
			} else {
				restoreIntoNewPanel(computeSnapshotFromTrail(trail));
			}
		},
		[source, panel, followLink, restoreIntoNewPanel, closeAllHovers],
	);
};

/**
 * Mounts the shell's `ShellRefLinkImpl`, the shell's navigate hook
 * impl, the shell's `RefStubResolver`, and the
 * `RefDropRegionProvider` (whose counter `DockviewLayout` consults
 * to suppress its drop overlay when a renderer-owned drop region is
 * being dragged over). `<PanelViewer>` installs this once near
 * the top of its tree; descendants — including dynamically-loaded
 * renderer modules and figures from `@fxtr-ui/reports` — pick up
 * the shell wiring automatically.
 *
 * One mount, every consumer: the navigate impl is a hook, so the
 * consumer's hook scope re-evaluates panel / breadcrumb / etc.
 * contexts on each call site.
 */
export function ShellRefLinkProvider({ children }: { readonly children: ReactNode }): ReactNode {
	return (
		<RefDropRegionProvider>
			<ShellRefStubResolverProvider>
				<RefLinkImplContext.Provider value={ShellRefLinkImpl}>
					<WorkstationNavigateContext.Provider value={useShellNavigate}>
						{children}
					</WorkstationNavigateContext.Provider>
				</RefLinkImplContext.Provider>
			</ShellRefStubResolverProvider>
		</RefDropRegionProvider>
	);
}

/**
 * useRefHoverCard — hover-preview wrapper for a ref (entity or
 * service).
 *
 * Returns a `Wrap` component: wrap any trigger element in it to
 * get a base-ui PreviewCard that renders
 * `<RenderDispatch stub={stub} as={HoverPreview} />` on hover.
 * Handles all the nested-hover coordination:
 *
 *   - Uses `HoverKeptOpenContext` so nested hovers keep the
 *     parent open.
 *   - Registers with `HoverCloseContext` so `followLink` /
 *     `restoreIntoNewPanel` can force-close.
 *   - Provides an extended `BreadcrumbSource` to the popup body
 *     so refs clicked inside it emit a trail that includes this
 *     hover step.
 */

import { PreviewCard } from "@base-ui/react/preview-card";
import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import { RenderDispatch } from "@fxtr-ui/react-dispatch";
import { HoverPreview } from "@fxtr-ui/workstation-dispatch";
import {
	type ComponentType,
	type ReactNode,
	Suspense,
	useCallback,
	useEffect,
	useMemo,
	useState,
} from "react";
import {
	BreadcrumbSourceProvider,
	useBreadcrumbSource,
} from "../breadcrumb/BreadcrumbSourceContext.js";
import type { Breadcrumb, BreadcrumbSource } from "../breadcrumb/types.js";
import { entryKind, panelViewEntryOf } from "../types.js";
import { HoverKeptOpenContext, useHoverClose, useSetKeptOpen } from "./HoverContext.js";

export interface HoverCardWrapProps {
	readonly children: ReactNode;
}

/**
 * Build a `Wrap` component for a given ref. `useMemo`able by the
 * caller.
 *
 * `as` picks the element the PreviewCard.Trigger renders as.
 * Using a real element (span for inline, div for block) is load-
 * bearing: base-ui's Positioner reads the trigger's
 * `getBoundingClientRect()` to anchor the popup, so a
 * `display: contents` trigger anchors at `{0,0,0,0}` and the
 * popup jumps to the document origin. (PreviewCard defaults to
 * `<a>`, which has a real box but wraps our interactive div/span
 * oddly — specifying the element type up-front avoids both
 * issues.)
 */
export function useRefHoverCard(
	stub: EntityRefStub | ServiceRefStub,
	as: "inline" | "block" = "block",
): ComponentType<HoverCardWrapProps> {
	return useMemo(() => makeHoverCardWrap(stub, as), [stub, as]);
}

function makeHoverCardWrap(
	stub: EntityRefStub | ServiceRefStub,
	as: "inline" | "block",
): ComponentType<HoverCardWrapProps> {
	function HoverCardWrap({ children }: HoverCardWrapProps) {
		const [open, setOpen] = useState(false);
		const [keptOpenByChild, setKeptOpenByChild] = useState(false);
		const [keptOpenByHover, setKeptOpenByHover] = useState(false);
		const [touched, setTouched] = useState(false);
		const parentSetKeptOpen = useSetKeptOpen();
		const hoverClose = useHoverClose();
		const parent = useBreadcrumbSource();

		const isOpen = touched && (open || keptOpenByChild || keptOpenByHover);

		// Extend the surrounding chain; or start a fresh hover-
		// originated chain if there's no parent source.
		const hoverSource = useMemo<BreadcrumbSource>(() => {
			const ref = panelViewEntryOf(stub);
			if (parent === null || parent.currentKey === null) {
				return {
					pendingBreadcrumbs: parent?.pendingBreadcrumbs ?? [],
					currentKey: ref,
					via: "hover-ref",
				};
			}
			const segment: Breadcrumb = {
				from: parent.currentKey,
				to: ref,
				via: parent.via,
			};
			return {
				pendingBreadcrumbs: [...parent.pendingBreadcrumbs, segment],
				currentKey: ref,
				via: "hover-ref",
			};
		}, [parent, stub]);

		// Bubble our open-state up to any outer hover-card.
		useEffect(() => {
			parentSetKeptOpen?.(isOpen);
		}, [isOpen, parentSetKeptOpen]);

		// Register as closeable so navigation imperatives can snap
		// us shut.
		useEffect(() => {
			if (hoverClose === null) return;
			return hoverClose.registerCloseListener(() => {
				setTouched(false);
				setOpen(false);
				setKeptOpenByChild(false);
				setKeptOpenByHover(false);
			});
		}, [hoverClose]);

		const onTriggerEnter = useCallback(() => setTouched(true), []);
		const onPopupEnter = useCallback(() => setKeptOpenByHover(true), []);
		const onPopupLeave = useCallback(() => setKeptOpenByHover(false), []);

		const triggerElement =
			as === "inline" ? <span style={inlineTriggerStyle} /> : <div style={blockTriggerStyle} />;

		return (
			<PreviewCard.Root open={isOpen} onOpenChange={setOpen}>
				<PreviewCard.Trigger
					delay={500}
					closeDelay={300}
					render={triggerElement}
					onMouseEnter={onTriggerEnter}
				>
					{children}
				</PreviewCard.Trigger>
				<PreviewCard.Portal>
					<PreviewCard.Positioner side="top" align="start" sideOffset={8}>
						<PreviewCard.Popup
							className="fw-hover-popup"
							onMouseEnter={onPopupEnter}
							onMouseLeave={onPopupLeave}
						>
							<HoverKeptOpenContext.Provider value={setKeptOpenByChild}>
								<BreadcrumbSourceProvider value={hoverSource}>
									<Suspense fallback={<div className="fw-hover-popup-loading">Loading…</div>}>
										<RenderDispatch stub={stub} as={HoverPreview} />
									</Suspense>
								</BreadcrumbSourceProvider>
							</HoverKeptOpenContext.Provider>
						</PreviewCard.Popup>
					</PreviewCard.Positioner>
				</PreviewCard.Portal>
			</PreviewCard.Root>
		);
	}
	const kind = entryKind(panelViewEntryOf(stub));
	HoverCardWrap.displayName = `HoverCardWrap(${kind}:${stub.shortId})`;
	return HoverCardWrap;
}

// Inline trigger wraps inline content; an inline-block keeps it
// sitting on the baseline with the surrounding text while giving
// it a real box the positioner can measure.
const inlineTriggerStyle = { display: "inline-block" } as const;
// Block trigger fills its row; `display: contents` would collapse
// the measurement target, so we use `block` + width:auto.
const blockTriggerStyle = { display: "block" } as const;

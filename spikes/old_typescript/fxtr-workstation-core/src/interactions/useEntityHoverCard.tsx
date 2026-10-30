/**
 * useEntityHoverCard — hover-preview wrapper for a ref.
 *
 * Returns a `Wrap` component: wrap any trigger element in it to get a
 * base-ui PreviewCard that renders `<Render entity={id} as={HoverPreview} />`
 * on hover. Handles all the nested-hover coordination:
 *
 * - Uses `HoverKeptOpenContext` so nested hovers stuck the parent open.
 * - Registers with `HoverCloseContext` so `navigate()` can force-close.
 * - Provides an extended `BreadcrumbSource` to the popup body so refs
 *   clicked inside it emit a trail that includes this hover step.
 *
 * Mirrors thingwatcher's `HoverPreviewCard`.
 */

import { PreviewCard } from "@base-ui/react/preview-card";
import type { FxtrID } from "@fxtr/core";
import { Render } from "@fxtr/react-dispatch";
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
import { HoverPreview } from "../keys.js";
import { HoverKeptOpenContext, useHoverClose, useSetKeptOpen } from "./HoverContext.js";

export interface HoverCardWrapProps {
	children: ReactNode;
}

/**
 * Build a `Wrap` component for a given entity. `useMemo`able by the caller.
 *
 * `as` picks the element the PreviewCard.Trigger renders as. Using a
 * real element (span for inline, div for block) is load-bearing:
 * base-ui's Positioner reads the trigger's `getBoundingClientRect()` to
 * anchor the popup, so a `display: contents` trigger anchors at
 * `{0,0,0,0}` and the popup jumps to the document origin. (PreviewCard
 * defaults to `<a>`, which has a real box but wraps our interactive
 * div/span oddly — specifying the element type up-front avoids both
 * issues.)
 */
export function useEntityHoverCard(
	entity: FxtrID,
	as: "inline" | "block" = "block",
): ComponentType<HoverCardWrapProps> {
	return useMemo(() => makeHoverCardWrap(entity, as), [entity, as]);
}

function makeHoverCardWrap(
	entity: FxtrID,
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

		// Extend the surrounding chain; or start a fresh hover-originated
		// chain if there's no parent source.
		const hoverSource = useMemo<BreadcrumbSource>(() => {
			if (!parent || parent.currentKey === null) {
				return {
					pendingBreadcrumbs: parent?.pendingBreadcrumbs ?? [],
					currentKey: entity,
					via: "hover-ref",
				};
			}
			const segment: Breadcrumb = {
				fromEntityId: parent.currentKey.idString(),
				toEntityId: entity.idString(),
				via: parent.via,
			};
			return {
				pendingBreadcrumbs: [...parent.pendingBreadcrumbs, segment],
				currentKey: entity,
				via: "hover-ref",
			};
		}, [parent, entity]);

		// Bubble our open-state up to any outer hover-card.
		useEffect(() => {
			parentSetKeptOpen?.(isOpen);
		}, [isOpen, parentSetKeptOpen]);

		// Register as closeable so navigate() can snap us shut.
		useEffect(() => {
			if (!hoverClose) return;
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
										<Render entity={entity} as={HoverPreview} />
									</Suspense>
								</BreadcrumbSourceProvider>
							</HoverKeptOpenContext.Provider>
						</PreviewCard.Popup>
					</PreviewCard.Positioner>
				</PreviewCard.Portal>
			</PreviewCard.Root>
		);
	}
	HoverCardWrap.displayName = `HoverCardWrap(${entity.idString()})`;
	return HoverCardWrap;
}

// Inline trigger wraps inline content; an inline-block keeps it sitting
// on the baseline with the surrounding text while giving it a real box
// the positioner can measure.
const inlineTriggerStyle = { display: "inline-block" } as const;
// Block trigger fills its row; `display: contents` would collapse the
// measurement target, so we use `block` + width:auto.
const blockTriggerStyle = { display: "block" } as const;

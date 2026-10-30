/**
 * `SidebarTooltip` — per-row tooltip for the open-panels
 * sidebar.
 *
 * Uses base-ui's `Tooltip` (not `PreviewCard`) because:
 *   - `Tooltip.Provider` gives shared-delay grouping for free:
 *     once one tooltip has opened, subsequent hovers on sibling
 *     triggers open instantly, so scanning a panel list feels
 *     snappy.
 *   - `disableHoverablePopup={true}` on `Tooltip.Root` closes the
 *     popup on trigger-leave; the cursor moving into the popup
 *     itself doesn't keep it open. Sidebar tooltips aren't meant
 *     to be deeply nested hover stacks — they just describe a
 *     panel you've already opened.
 *
 * Popup content reuses the normal `HoverPreview` dispatch so
 * consumers only register a preview renderer once — it's picked
 * up by both the standard hover-card (via `useRefHoverCard`) and
 * by the sidebar tooltip.
 */

import { Tooltip } from "@base-ui/react/tooltip";
import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import { RenderDispatch } from "@fxtr-ui/react-dispatch";
import { HoverPreview } from "@fxtr-ui/workstation-dispatch";
import { type ReactNode, Suspense } from "react";

/**
 * Re-export of `Tooltip.Provider` for clarity at the sidebar
 * call site. Wrap the panel list in this so its tooltips share
 * a delay.
 */
export const SidebarTooltipProvider = Tooltip.Provider;

export interface SidebarTooltipProps {
	readonly stub: EntityRefStub | ServiceRefStub;
	readonly children: ReactNode;
	/**
	 * When `true`, the row represents a `display: "metadata"`
	 * chain entry. The tooltip skips the `HoverPreview` dispatch
	 * (it would track the ref in the dispatcher session — the
	 * exact thing the metadata view exists to avoid) and shows a
	 * plain text body instead.
	 */
	readonly isMetadata?: boolean;
}

export function SidebarTooltip({ stub, children, isMetadata }: SidebarTooltipProps) {
	return (
		<Tooltip.Root disableHoverablePopup>
			<Tooltip.Trigger render={<span style={triggerStyle} />}>{children}</Tooltip.Trigger>
			<Tooltip.Portal>
				<Tooltip.Positioner side="right" align="center" sideOffset={8}>
					<Tooltip.Popup className="fw-hover-popup">
						{isMetadata === true ? (
							<div style={metadataBodyStyle}>
								Metadata: <code>{stub.shortId}</code>
							</div>
						) : (
							<Suspense fallback={<div className="fw-hover-popup-loading">Loading…</div>}>
								<RenderDispatch stub={stub} as={HoverPreview} />
							</Suspense>
						)}
					</Tooltip.Popup>
				</Tooltip.Positioner>
			</Tooltip.Portal>
		</Tooltip.Root>
	);
}

// The trigger needs a real box for the positioner (same story
// as `useRefHoverCard` — see its comment). A block-level
// inline-block span fills the sidebar row cell while still
// letting the row's own click handler see the click events.
const triggerStyle = { display: "block", minWidth: 0 } as const;

const metadataBodyStyle = {
	padding: "8px 12px",
	color: "var(--text-secondary)",
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
} as const;

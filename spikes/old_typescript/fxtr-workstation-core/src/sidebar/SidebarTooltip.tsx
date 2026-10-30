/**
 * SidebarTooltip — per-row tooltip for the open-panels sidebar.
 *
 * Uses base-ui's `Tooltip` (not `PreviewCard`) because:
 * - `Tooltip.Provider` gives shared-delay grouping for free: once one
 *   tooltip has opened, subsequent hovers on sibling triggers open
 *   instantly, so scanning a panel list feels snappy.
 * - `disableHoverablePopup={true}` on `Tooltip.Root` closes the popup
 *   on trigger-leave; the cursor moving into the popup itself doesn't
 *   keep it open. Sidebar tooltips aren't meant to be deeply nested
 *   hover stacks — they just tell you about a panel you've already
 *   opened.
 *
 * Mirrors thingwatcher's `components/Sidebar/SidebarTooltip.tsx`.
 *
 * Popup content uses the normal `HoverPreview` dispatch so consumers
 * only need to register a preview renderer once — it's picked up by
 * both the standard hover-card (via `useEntityHoverCard`) and by the
 * sidebar tooltip.
 */

import { Tooltip } from "@base-ui/react/tooltip";
import type { FxtrID } from "@fxtr/core";
import { Render } from "@fxtr/react-dispatch";
import { type ReactNode, Suspense } from "react";
import { HoverPreview } from "../keys.js";

/**
 * Re-export of `Tooltip.Provider` for clarity at the sidebar call
 * site. Wrap the panel list in this so its tooltips share a delay.
 */
export const SidebarTooltipProvider = Tooltip.Provider;

export interface SidebarTooltipProps {
	entity: FxtrID;
	children: ReactNode;
}

export function SidebarTooltip({ entity, children }: SidebarTooltipProps) {
	return (
		<Tooltip.Root disableHoverablePopup>
			<Tooltip.Trigger render={<span style={triggerStyle} />}>{children}</Tooltip.Trigger>
			<Tooltip.Portal>
				<Tooltip.Positioner side="right" align="center" sideOffset={8}>
					<Tooltip.Popup className="fw-hover-popup">
						<Suspense fallback={<div className="fw-hover-popup-loading">Loading…</div>}>
							<Render entity={entity} as={HoverPreview} />
						</Suspense>
					</Tooltip.Popup>
				</Tooltip.Positioner>
			</Tooltip.Portal>
		</Tooltip.Root>
	);
}

// The trigger needs a real box for the positioner (same story as
// `useEntityHoverCard` — see its comment). A block-level inline-block
// span fills the sidebar row cell while still letting the row's own
// click handler see the click events.
const triggerStyle = { display: "block", minWidth: 0 } as const;

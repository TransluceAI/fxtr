/**
 * `DispatcherDebugPanel` — dockview content renderer for the
 * dispatcher's optional debug surface.
 *
 * Reads the dispatcher from `<DispatcherProvider>` context and
 * renders the element returned from `dispatcher.debugView()`. The
 * debug element wraps itself in a jotai `<Provider store={...}>`
 * scoped to the dispatcher's session store, so atom reads inside
 * the debug component observe the dispatcher's own updates
 * regardless of the surrounding store context.
 *
 * When the dispatcher's `debugView` is undefined, the
 * `WorkstationDebugPanel`'s "Open dispatcher debug" button hides
 * itself — so this panel only gets opened for dispatchers that
 * actually have a debug surface. The empty-state branch here is
 * defensive against a layout-restore that brings back the panel
 * after the dispatcher implementation has been swapped.
 *
 * Does NOT provide `PanelContext`. Click / drag on debug-surface
 * ref chips, if any, route through `restoreIntoNewPanel`, opening
 * a new ref panel rather than mutating a source panel's chain.
 */

import { useDispatcher } from "@fxtr-ui/react-dispatch";
import type { IDockviewPanelProps } from "dockview";
import type { CSSProperties } from "react";
import type { DispatcherDebugPanelParams } from "./DispatcherDebugPanelParams.js";

export function DispatcherDebugPanel(_props: IDockviewPanelProps<DispatcherDebugPanelParams>) {
	const dispatcher = useDispatcher();
	const view = dispatcher.debugView?.();
	return (
		<div className="fw-panel" style={bodyStyle}>
			<h2 style={titleStyle}>Dispatcher debug</h2>
			{view ?? (
				<p style={emptyStyle}>
					This dispatcher doesn't expose a <code>debugView</code> method. (The panel was likely
					restored from a saved layout after the dispatcher implementation changed.)
				</p>
			)}
		</div>
	);
}

const bodyStyle: CSSProperties = {
	height: "100%",
	overflow: "auto",
	padding: "var(--sp-4)",
	fontFamily: "var(--font-prose)",
	color: "var(--text-primary)",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-3, 12px)",
};

const titleStyle: CSSProperties = {
	margin: 0,
	fontSize: "var(--text-heading, 18px)",
	color: "var(--text-primary)",
};

const emptyStyle: CSSProperties = {
	color: "var(--text-muted, #888)",
	fontSize: "var(--text-body-sm, 12px)",
};

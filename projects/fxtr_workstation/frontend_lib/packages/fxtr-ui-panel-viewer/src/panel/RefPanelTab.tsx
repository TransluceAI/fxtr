/**
 * Custom dockview tab for ref panels. Renders the standard
 * `DockviewDefaultTab` but intercepts the close X click via
 * `closeActionOverride`, routing it through
 * `PanelViewerApi.closePanel` so the panel's `WorkstationSession`
 * is terminated server-side before the dockview panel is removed.
 *
 * Used as `defaultTabComponent` in `DockviewLayout` so every panel
 * kind gets the same treatment (non-ref panels have no session;
 * `closePanel` just removes them).
 *
 * Why an override and not `onDidRemovePanel`: dockview's
 * `onDidRemovePanel` fires for both user-initiated removals AND
 * for component-disposal teardowns (React StrictMode dev cycle,
 * real shell unmount). The tab-button close, sidebar X click, and
 * any other user gesture are the only "this was the user's
 * choice" signals — `closeActionOverride` catches exactly those.
 */

import { DockviewDefaultTab, type IDockviewPanelHeaderProps } from "dockview";
import { useCallback } from "react";
import { usePanelViewer } from "../shell/PanelViewerContext.js";

export function RefPanelTab(props: IDockviewPanelHeaderProps) {
	const { api } = props;
	const { closePanel } = usePanelViewer();
	const onCloseOverride = useCallback(() => {
		closePanel(api.id);
	}, [api.id, closePanel]);
	return <DockviewDefaultTab {...props} closeActionOverride={onCloseOverride} />;
}

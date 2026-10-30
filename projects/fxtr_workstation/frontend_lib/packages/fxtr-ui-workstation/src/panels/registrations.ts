/**
 * `workstationPanelRegistrations` — the four built-in workstation panels
 * expressed as injected `PanelRegistration`s.
 *
 * The generic panel viewer (`PanelViewer` today, `PanelViewer` after
 * the package move) owns only the built-in **ref panel**; every other
 * panel is supplied by the app as a registration. This array is the
 * workstation app's set — the demos import it and pass it to the viewer
 * via `panelRegistrations`, which is the seam that keeps the viewer from
 * importing these panels directly.
 *
 * The panel components read the connector / dispatcher off React context
 * (`useConnector` / `useDispatcher`), so a single static array works for
 * every workstation-connector demo.
 */

import type { PanelRegistration } from "@fxtr-ui/panel-viewer";
import { DispatcherDebugPanel } from "./DispatcherDebugPanel.js";
import { DISPATCHER_DEBUG_PANEL_COMPONENT } from "./DispatcherDebugPanelParams.js";
import { RootsPanel } from "./RootsPanel.js";
import { ROOTS_PANEL_COMPONENT } from "./RootsPanelParams.js";
import { SessionDebugPanel } from "./SessionDebugPanel.js";
import { SESSION_DEBUG_PANEL_COMPONENT } from "./SessionDebugPanelParams.js";
import { WorkstationDebugPanel } from "./WorkstationDebugPanel.js";
import { WORKSTATION_DEBUG_PANEL_COMPONENT } from "./WorkstationDebugPanelParams.js";

export const workstationPanelRegistrations: readonly PanelRegistration[] = [
	{
		id: ROOTS_PANEL_COMPONENT,
		title: "Roots",
		component: RootsPanel,
		sidebarButton: { label: "Roots" },
	},
	{
		id: WORKSTATION_DEBUG_PANEL_COMPONENT,
		title: "Debug",
		component: WorkstationDebugPanel,
		sidebarButton: { label: "Debug" },
	},
	{
		id: SESSION_DEBUG_PANEL_COMPONENT,
		// Per-session panel: label the tab with the short session id.
		title: (params) => `Session ${String(params.sessionId).slice(0, 8)}…`,
		component: SessionDebugPanel,
		forSession: true,
	},
	{
		id: DISPATCHER_DEBUG_PANEL_COMPONENT,
		title: "Dispatcher debug",
		component: DispatcherDebugPanel,
	},
];

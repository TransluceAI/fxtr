/**
 * Public surface of `@fxtr-ui/workstation`.
 *
 * Reduced to the **workstation-connector panels** expressed as injected
 * `PanelRegistration`s (roots + the three debug panels) plus their debug
 * tables. The generic dockview panel-viewer mechanics it used to contain
 * now live in `@fxtr-ui/panel-viewer`; this package depends on that and
 * **re-exports its full surface** for back-compat, so existing consumers
 * importing the shell / navigation / dispatch helpers from
 * `@fxtr-ui/workstation` keep resolving.
 */

// ----- Re-export the generic panel-viewer surface (back-compat) -----
export * from "@fxtr-ui/panel-viewer";

// ----- Workstation-connector narrowing hook -----
export { useWorkstationConnector } from "./useWorkstationConnector.js";

// ----- Roots panel -------------------------------------------------------
export { RootsPanel } from "./panels/RootsPanel.js";
export {
	ROOTS_PANEL_COMPONENT,
	type RootsPanelParams,
} from "./panels/RootsPanelParams.js";

// ----- Workstation debug panel -------------------------------------------
export { WorkstationDebugPanel } from "./panels/WorkstationDebugPanel.js";
export {
	WORKSTATION_DEBUG_PANEL_COMPONENT,
	type WorkstationDebugPanelParams,
} from "./panels/WorkstationDebugPanelParams.js";
export { SessionDebugPanel } from "./panels/SessionDebugPanel.js";
export {
	SESSION_DEBUG_PANEL_COMPONENT,
	type SessionDebugPanelParams,
} from "./panels/SessionDebugPanelParams.js";
export { DispatcherDebugPanel } from "./panels/DispatcherDebugPanel.js";
export {
	DISPATCHER_DEBUG_PANEL_COMPONENT,
	type DispatcherDebugPanelParams,
} from "./panels/DispatcherDebugPanelParams.js";

// ----- Built-in panel registrations --------------------------------------
// The four workstation panels expressed as injected registrations. Demos
// pass this to `<PanelViewer panelRegistrations={...} />`.
export { workstationPanelRegistrations } from "./panels/registrations.js";

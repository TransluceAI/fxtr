/**
 * Dockview params for the dispatcher-debug panel.
 *
 * Stateless: the panel reads the dispatcher off
 * `<DispatcherProvider>` context and renders the element returned
 * from `dispatcher.debugView()`. No session-id, no per-panel chain
 * state. The shell's `openDispatcherDebugPanel` helper opens at
 * most one of these — find-or-focus semantics paralleling the
 * workstation-debug panel.
 *
 * The panel only makes sense for dispatchers that expose a
 * `debugView()` method (the optional surface on
 * `@fxtr-ui/react-dispatch`'s `Dispatcher` interface). The
 * `WorkstationDebugPanel`'s "Open dispatcher debug" button hides
 * itself when the dispatcher leaves `debugView` undefined, so a
 * `DispatcherDebugPanel` doesn't appear in the layout in that
 * case.
 */

export interface DispatcherDebugPanelParams {
	readonly kind: "dispatcher_debug";
}

/**
 * Dockview panel-component name. Wire-stable: persisted in the
 * dockview layout JSON.
 */
export const DISPATCHER_DEBUG_PANEL_COMPONENT = "dispatcher-debug";

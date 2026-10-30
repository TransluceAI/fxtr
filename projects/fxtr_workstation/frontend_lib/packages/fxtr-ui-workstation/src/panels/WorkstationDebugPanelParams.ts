/**
 * Dockview params for the workstation-level debug panel.
 *
 * Stateless: the panel subscribes to the connector's four resource
 * lists with `debug=true` and renders them as tables. No
 * session-id, no per-panel chain state. The shell's
 * `focusOrOpenWorkstationDebug` helper opens at most one of
 * these — find-or-focus semantics paralleling `RootsPanel`.
 */

export interface WorkstationDebugPanelParams {
	readonly kind: "workstation_debug";
}

/**
 * Dockview panel-component name. Wire-stable: persisted in the
 * dockview layout JSON.
 */
export const WORKSTATION_DEBUG_PANEL_COMPONENT = "workstation-debug";

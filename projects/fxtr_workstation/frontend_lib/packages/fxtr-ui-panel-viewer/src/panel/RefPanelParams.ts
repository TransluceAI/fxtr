/**
 * The JSON-serializable params dockview stores for a ref panel.
 *
 * The only durable per-panel datum is the panel's session id (or
 * `null` while the session is being created). Everything else —
 * chain, active index, history root, dispatched-renderer's own
 * storage — lives inside the panel's session userState and is
 * recovered on reattach.
 *
 * `kind: "ref"` distinguishes ref panels from other dockview panel
 * kinds (e.g. the roots panel). The shell switches on `kind` when
 * registering panel content renderers.
 */

export interface RefPanelParams {
	readonly kind: "ref";
	readonly sessionId: string | null;
}

/**
 * The dockview panel-component name registered for ref panels.
 * Wire-stable: persisted in dockview layout JSON.
 */
export const REF_PANEL_COMPONENT = "ref";

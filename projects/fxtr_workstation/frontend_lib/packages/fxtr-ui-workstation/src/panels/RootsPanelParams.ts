/**
 * The dockview params for the roots panel — the registry browser
 * that lists externally-named entities and services from the
 * workstation.
 *
 * Stateless: the roots panel has no per-instance state to
 * persist, so its params carry only the `kind` discriminator.
 * No session, no `sessionId`. The roots view is a view over
 * connector-scope reactive lists; nothing it shows is
 * panel-specific.
 *
 * This is the deliberate split between **ref panels** (which
 * own a session for chain / history / per-renderer storage) and
 * **workstation-level info panels** like roots (which don't).
 * Future info panels — settings, debug, etc. — can follow the
 * same shape.
 */

export interface RootsPanelParams {
	readonly kind: "roots";
}

/**
 * The dockview panel-component name registered for the roots
 * panel. Wire-stable: persisted in dockview layout JSON.
 */
export const ROOTS_PANEL_COMPONENT = "roots";

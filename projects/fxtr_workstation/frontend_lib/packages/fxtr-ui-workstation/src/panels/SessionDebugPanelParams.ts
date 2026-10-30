/**
 * Dockview params for the per-session debug panel.
 *
 * Stateless w.r.t. its own session: the panel attaches to the
 * target session for read + terminate, and subscribes to that
 * session's raw events + debug-snapshot streams. The sessionId is
 * the only durable per-panel datum and rides in the params.
 *
 * The shell's `openSessionDebugPanel(sessionId, position)` opens
 * (or focuses an existing) panel keyed by sessionId — at most one
 * debug panel per target session at a time.
 */

export interface SessionDebugPanelParams {
	readonly kind: "session_debug";
	readonly sessionId: string;
}

/**
 * Dockview panel-component name. Wire-stable: persisted in the
 * dockview layout JSON.
 */
export const SESSION_DEBUG_PANEL_COMPONENT = "session-debug";

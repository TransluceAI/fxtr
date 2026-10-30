/**
 * `SessionUnavailable` — terminal-state body for a panel whose
 * session can't be reached.
 *
 * Rendered when `usePanelSession`'s state machine flips to
 * `"unavailable"` — the workstation server returned a
 * `subscribe_failed` for the panel's persisted session id. Causes
 * include: the server was restarted, the session id was deleted
 * by another consumer's `terminate()`, or the layout was restored
 * with an id that was never valid.
 *
 * The body is informational only — no "reopen" affordance yet.
 * TODO: add a reopen button that resets the panel to
 * `sessionId: null` so the lifecycle hook re-fires `createSession`.
 * Tracked in the stage-8 plan's cleanup log.
 */

import type { CSSProperties } from "react";

export interface SessionUnavailableProps {
	readonly sessionId: string;
}

const root: CSSProperties = {
	height: "100%",
	padding: "var(--sp-4)",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2)",
	background: "var(--surface-card)",
	color: "var(--text-primary)",
	fontFamily: "var(--font-prose)",
};
const heading: CSSProperties = {
	fontWeight: 600,
	fontSize: "var(--text-body)",
};
const detail: CSSProperties = {
	fontSize: "var(--text-body-sm)",
	color: "var(--text-secondary)",
	lineHeight: 1.5,
};
const idStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	background: "var(--surface-inset, var(--surface-sunken))",
	padding: "0 var(--sp-1)",
	borderRadius: "var(--radius-badge)",
};

export function SessionUnavailable({ sessionId }: SessionUnavailableProps) {
	return (
		<div style={root}>
			<div style={heading}>Session no longer available</div>
			<div style={detail}>
				The workstation session <code style={idStyle}>{sessionId}</code> isn't recognized by the
				server. It may have been removed since this panel was last saved, or the server has
				restarted. Close this panel and open a new one to resume work here.
			</div>
		</div>
	);
}

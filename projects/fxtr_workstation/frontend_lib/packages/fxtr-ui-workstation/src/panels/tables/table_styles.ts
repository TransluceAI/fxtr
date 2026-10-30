/**
 * Shared style constants for the workstation-debug tables. Kept
 * in one place so the four tables (rpc_connections / services /
 * entities / sessions) have a consistent look without each
 * redefining the same tokens.
 */

import type { CSSProperties } from "react";

export const headerStyle: CSSProperties = {
	textTransform: "uppercase",
	fontSize: "var(--text-caption)",
	letterSpacing: "0.05em",
	color: "var(--text-muted)",
	fontFamily: "var(--font-prose)",
};

export const tableStyle: CSSProperties = {
	borderCollapse: "collapse",
	width: "100%",
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-primary)",
};

export const thStyle: CSSProperties = {
	textAlign: "left",
	padding: "var(--sp-1) var(--sp-2)",
	borderBottom: "1px solid var(--border-subtle)",
	color: "var(--text-muted)",
	fontWeight: 600,
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-caption)",
};

export const rowKeyStyle: CSSProperties = {
	padding: "var(--sp-1) var(--sp-2)",
	color: "var(--text-secondary)",
	whiteSpace: "nowrap",
};

export const mutedStyle: CSSProperties = {
	color: "var(--text-muted)",
	fontStyle: "italic",
	fontFamily: "var(--font-prose)",
};

export const chipStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	padding: "0 6px",
	borderRadius: "var(--radius-badge)",
	background: "var(--surface-raised)",
	border: "1px solid var(--border-subtle)",
	color: "var(--text-secondary)",
	fontFamily: "var(--font-mono)",
	fontSize: "11px",
	whiteSpace: "nowrap",
};

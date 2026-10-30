/**
 * Shared style tokens for the report renderers.
 *
 * Per-renderer reactive setup (effect + result atom + figure
 * storage) is intentionally NOT factored out — each renderer's
 * `useMemo` is the place to wire that, calling
 * `makeLoadResultAtom` and `makeFigureStorage` directly.
 */

import type { CSSProperties } from "react";

export const mutedStyle: CSSProperties = {
	color: "var(--text-muted, #66707a)",
	fontSize: "var(--text-body-sm, 13px)",
	fontStyle: "italic",
};

export const errorStyle: CSSProperties = {
	color: "var(--status-error, #c00)",
	fontSize: "var(--text-body-sm, 13px)",
};

export const panelBodyStyle: CSSProperties = {
	padding: "var(--sp-5, 16px)",
	overflow: "auto",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-3, 12px)",
	fontFamily: "var(--font-prose)",
	color: "var(--text-primary)",
};

export const reportTitleStyle: CSSProperties = {
	margin: 0,
	fontSize: "var(--text-heading-lg, 24px)",
	color: "var(--text-primary)",
};

export const reportSummaryStyle: CSSProperties = {
	margin: 0,
	color: "var(--text-secondary, #4a5560)",
	fontSize: "var(--text-body)",
	lineHeight: 1.5,
};

export const sectionLabelStyle: CSSProperties = {
	margin: 0,
	fontSize: "var(--text-heading, 18px)",
	color: "var(--text-primary)",
};

export const linkPillStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: "var(--sp-2)",
	padding: "var(--sp-1) var(--sp-3)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-badge)",
	background: "var(--surface-raised)",
	fontSize: "var(--text-body-sm)",
};

export const linkTagStyle: CSSProperties = {
	textTransform: "uppercase",
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
	letterSpacing: "0.05em",
};

export const linkLabelStyle: CSSProperties = {
	color: "var(--text-primary)",
};

export const hoverCardStyle: CSSProperties = {
	minWidth: 240,
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2, 8px)",
	padding: "var(--sp-3, 12px)",
	fontFamily: "var(--font-prose)",
};

export const hoverTitleStyle: CSSProperties = {
	fontWeight: 600,
	color: "var(--text-primary)",
};

export const hoverBodyStyle: CSSProperties = {
	color: "var(--text-secondary)",
	fontSize: "var(--text-body-sm)",
	lineHeight: 1.4,
};

// ---- Figure-mode shell (shaded container + header + raised body) ----

export const figureShellStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2, 8px)",
	padding: "var(--sp-3, 12px)",
	background: "var(--surface-sunken, #f6f7f9)",
	border: "1px solid var(--border-subtle, #e0e3e8)",
	borderRadius: "var(--radius-card, 6px)",
	margin: "var(--sp-3, 12px) 0",
};

export const figureHeaderStyle: CSSProperties = {
	display: "flex",
	alignItems: "center",
	gap: "var(--sp-2, 8px)",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-caption, 12px)",
	color: "var(--text-muted, #66707a)",
	textTransform: "uppercase",
	letterSpacing: "0.05em",
};

export const figureBodyStyle: CSSProperties = {
	background: "var(--surface-raised, #ffffff)",
	border: "1px solid var(--border-subtle, #e0e3e8)",
	borderRadius: "var(--radius-card, 6px)",
	padding: "var(--sp-3, 12px)",
};

/**
 * Variant of `figureShellStyle` for renderers that make the whole
 * figure a click target (navigate to the embedded entity). Adds
 * cursor + outline-focus affordances for keyboard navigation.
 */
export const figureShellInteractiveStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2, 8px)",
	padding: "var(--sp-3, 12px)",
	background: "var(--surface-sunken, #f6f7f9)",
	border: "1px solid var(--border-subtle, #e0e3e8)",
	borderRadius: "var(--radius-card, 6px)",
	margin: "var(--sp-3, 12px) 0",
	cursor: "pointer",
};

/**
 * Generic dispatch error fallback. Rendered by the static
 * dispatch helpers when the resolved component throws or when
 * the dispatch state machine itself terminates in `errored`
 * (e.g. load failed, metadata failed, type-id missing).
 *
 * Stub-kind-agnostic: works for both entity and service refs.
 * The registry helper (`createWorkstationDispatchRegistry`) casts
 * it into the per-`(Stub, Props)` shape each static helper
 * expects — the extra Props beyond `stub` + `error` are dropped
 * at render time.
 *
 * Themed via the workstation CSS tokens. Renders inline; suitable
 * for any of the four dispatch keys (FullPanel / BlockLink /
 * InlineLink / HoverPreview) — looks compact enough to fit inside
 * a hover card and informative enough for a full panel body.
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import type { CSSProperties } from "react";

export interface DispatchErrorFallbackProps {
	readonly stub: EntityRefStub | ServiceRefStub;
	readonly error: Error;
}

const root: CSSProperties = {
	display: "inline-flex",
	flexDirection: "column",
	gap: "var(--sp-1)",
	padding: "var(--sp-2) var(--sp-3)",
	border: "1px solid var(--status-error, #c00)",
	borderRadius: "var(--radius-card)",
	background: "var(--status-error-bg, #fff5f5)",
	color: "var(--text-primary)",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body-sm)",
	maxWidth: "100%",
};
const heading: CSSProperties = {
	fontWeight: 600,
	color: "var(--status-error, #c00)",
};
const detail: CSSProperties = {
	fontFamily: "var(--font-mono)",
	color: "var(--text-secondary)",
	whiteSpace: "pre-wrap",
	wordBreak: "break-word",
};
const shortIdStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
};

export function DispatchErrorFallback(props: DispatchErrorFallbackProps) {
	const { stub, error } = props;
	const message = error.message || String(error);
	return (
		<div style={root}>
			<span style={heading}>render failed</span>
			<span style={shortIdStyle}>{stub.shortId}</span>
			<span style={detail}>{message}</span>
		</div>
	);
}

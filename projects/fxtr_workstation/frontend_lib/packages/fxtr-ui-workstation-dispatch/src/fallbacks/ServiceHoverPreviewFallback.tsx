/**
 * Default service-side HoverPreview fallback. Rendered inside a
 * base-ui PreviewCard when a ServiceRefStub is hovered and no
 * consumer-supplied HoverPreview renderer is registered.
 */

import type { ServiceRefStub } from "@fxtr-ui/core";
import type { CSSProperties } from "react";

export interface ServiceHoverPreviewFallbackProps {
	readonly stub: ServiceRefStub;
}

const card: CSSProperties = {
	padding: "var(--sp-2, 8px) var(--sp-3, 12px)",
	border: "1px solid var(--surface-line, #d8d8d8)",
	borderRadius: "var(--radius-md, 6px)",
	background: "var(--surface-card, #fff)",
	boxShadow: "0 2px 8px rgba(0, 0, 0, 0.08)",
	fontSize: "12px",
	minWidth: "180px",
};
const idLine: CSSProperties = {
	fontFamily: "var(--font-mono, ui-monospace, Menlo, monospace)",
	color: "var(--text-body, #222)",
};
const metaLine: CSSProperties = {
	color: "var(--text-muted, #888)",
	marginTop: "4px",
};

export function ServiceHoverPreviewFallback(props: ServiceHoverPreviewFallbackProps) {
	const { stub } = props;
	return (
		<div style={card}>
			<div style={idLine}>{stub.shortId}</div>
			<div style={metaLine}>service · no preview configured</div>
		</div>
	);
}

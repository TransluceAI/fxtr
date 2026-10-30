/**
 * Minimal `HoverPreview` body rendered while the dispatcher is
 * still resolving the service's type. Distinct from
 * `ServiceHoverPreviewFallback` (the no-match terminal): the
 * loading body avoids the "no preview configured" line that
 * would be misleading while the dispatcher might still be about
 * to land on a registered renderer.
 */

import type { ServiceRefStub } from "@fxtr-ui/core";
import type { CSSProperties } from "react";

export function ServiceHoverPreviewLoadingFallback(props: { stub: ServiceRefStub }) {
	const { stub } = props;
	return (
		<div style={card}>
			<div style={idLine}>{stub.shortId}</div>
			<div style={metaLine}>service · loading…</div>
		</div>
	);
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
	fontStyle: "italic",
};

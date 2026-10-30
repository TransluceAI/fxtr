/**
 * Default `HoverPreview` rendering of an entity stub when no
 * consumer renderer is registered for its type. Compact card
 * shown in the base-ui PreviewCard popup: short id on one line,
 * "unregistered preview" meta line beneath.
 */

import type { EntityRefStub } from "@fxtr-ui/core";
import type { CSSProperties } from "react";

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

export function EntityHoverPreviewFallback(props: { stub: EntityRefStub }) {
	const { stub } = props;
	return (
		<div style={card}>
			<div style={idLine}>{stub.shortId}</div>
			<div style={metaLine}>unregistered preview</div>
		</div>
	);
}

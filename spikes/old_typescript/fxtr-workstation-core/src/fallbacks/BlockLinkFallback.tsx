import type { EntityRef, UnknownEntity } from "@fxtr/core";
import type { CSSProperties } from "react";
import type { LinkProps } from "../keys.js";
import { shortId } from "../shortId.js";

export interface BlockLinkFallbackProps extends LinkProps {
	entityRef: EntityRef<UnknownEntity>;
}

// Sized to match the shape of a typical user BlockLink (uppercase tag +
// content label); tokens here line up with the demo's DemoNoteBlockLink
// so rows in the namespace panel look consistent across registered and
// fallback types.
const wrap: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: "var(--sp-2)",
	padding: "var(--sp-1) var(--sp-3)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-badge)",
	background: "var(--surface-raised)",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body-sm)",
};
const tag: CSSProperties = {
	textTransform: "uppercase",
	fontSize: "var(--text-caption)",
	letterSpacing: "0.05em",
	color: "var(--text-muted)",
};
const id: CSSProperties = {
	fontFamily: "var(--font-mono)",
	color: "var(--text-primary)",
};

export function BlockLinkFallback(props: BlockLinkFallbackProps) {
	const { entityRef, linkText } = props;
	return (
		<span style={wrap}>
			<span style={tag}>link</span>
			<span style={id}>{linkText ?? shortId(entityRef.entityId)}</span>
		</span>
	);
}

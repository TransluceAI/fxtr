/**
 * Default `BlockLink` rendering of an entity stub when no
 * consumer renderer is registered for its type. Uppercase "link"
 * tag + short id (or the caller-supplied `linkText`); sized to
 * match the shape of a typical user BlockLink so rows in the
 * roots panel look consistent across registered and fallback
 * types.
 */

import type { EntityRefStub } from "@fxtr-ui/core";
import type { CSSProperties } from "react";
import type { LinkProps } from "../dispatch/keys.js";

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

export function EntityBlockLinkFallback(props: { stub: EntityRefStub } & LinkProps) {
	const { stub, linkText } = props;
	return (
		<span style={wrap}>
			<span style={tag}>link</span>
			<span style={id}>{linkText ?? stub.shortId}</span>
		</span>
	);
}

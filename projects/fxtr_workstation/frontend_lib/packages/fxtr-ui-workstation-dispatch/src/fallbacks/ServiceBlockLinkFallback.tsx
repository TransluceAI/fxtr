/**
 * Default service-side BlockLink fallback. Rendered inline as a
 * tag-chip + short-id badge for a ServiceRefStub when no
 * consumer-supplied BlockLink renderer is registered.
 */

import type { ServiceRefStub } from "@fxtr-ui/core";
import type { CSSProperties } from "react";
import type { LinkProps } from "../dispatch/keys.js";

export type ServiceBlockLinkFallbackProps = { stub: ServiceRefStub } & LinkProps;

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

export function ServiceBlockLinkFallback(props: ServiceBlockLinkFallbackProps) {
	const { stub, linkText } = props;
	return (
		<span style={wrap}>
			<span style={tag}>service</span>
			<span style={id}>{linkText ?? stub.shortId}</span>
		</span>
	);
}

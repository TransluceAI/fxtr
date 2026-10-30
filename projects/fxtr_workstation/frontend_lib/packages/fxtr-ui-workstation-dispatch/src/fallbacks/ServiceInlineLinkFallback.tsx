/**
 * Default service-side InlineLink fallback. Renders the short id
 * as a `<code>` chip with an `svc:` prefix so it's visually
 * distinguishable from entity inline links inline-in-prose.
 */

import type { ServiceRefStub } from "@fxtr-ui/core";
import type { CSSProperties } from "react";
import type { LinkProps } from "../dispatch/keys.js";

export type ServiceInlineLinkFallbackProps = { stub: ServiceRefStub } & LinkProps;

const codeStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "0.9em",
	padding: "1px 4px",
	borderRadius: "var(--radius-badge)",
	background: "var(--surface-sunken)",
	color: "var(--text-secondary)",
};

export function ServiceInlineLinkFallback(props: ServiceInlineLinkFallbackProps) {
	const { stub, linkText } = props;
	return <code style={codeStyle}>svc:{linkText ?? stub.shortId}</code>;
}

/**
 * Default `InlineLink` rendering of an entity stub when no
 * consumer renderer is registered for its type. Compact `<code>`
 * with the short id (or caller-supplied `linkText`) — fits inline
 * inside prose / JsonTree leaves without disturbing line height.
 */

import type { EntityRefStub } from "@fxtr-ui/core";
import type { CSSProperties } from "react";
import type { LinkProps } from "../dispatch/keys.js";

const codeStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "0.9em",
	padding: "1px 4px",
	borderRadius: "var(--radius-badge)",
	background: "var(--surface-sunken)",
	color: "var(--text-accent)",
};

export function EntityInlineLinkFallback(props: { stub: EntityRefStub } & LinkProps) {
	const { stub, linkText } = props;
	return <code style={codeStyle}>{linkText ?? stub.shortId}</code>;
}

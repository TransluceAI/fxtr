import type { EntityRef, UnknownEntity } from "@fxtr/core";
import type { CSSProperties } from "react";
import type { LinkProps } from "../keys.js";
import { shortId } from "../shortId.js";

export interface InlineLinkFallbackProps extends LinkProps {
	entityRef: EntityRef<UnknownEntity>;
}

const codeStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "0.9em",
	padding: "1px 4px",
	borderRadius: "var(--radius-badge)",
	background: "var(--surface-sunken)",
	color: "var(--text-accent)",
};

export function InlineLinkFallback(props: InlineLinkFallbackProps) {
	const { entityRef, linkText } = props;
	return <code style={codeStyle}>{linkText ?? shortId(entityRef.entityId)}</code>;
}
